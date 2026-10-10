"""`GradingTriggerOutboxHandler` dispatch tests: routes each finalized
attempt's draft answers to the right grader by `item_type`, skips
gracefully on blank answers / missing assignments / unapproved candidates,
and ignores outbox entries of any other kind.

Repositories are constructed internally from `db`, so — per the "monkeypatch
the consuming module" lesson (see /memories/repo notes) — the classes are
patched on `outbox_handler` itself rather than on the modules that define
them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import app.domain.judge.outbox_handler as outbox_handler_module
from app.domain.exams.models import Draft, OutboxEntry
from app.domain.generation.models import CandidateStatus, GenerationCandidate, ItemType
from app.domain.judge.outbox_handler import GradingTriggerOutboxHandler

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _FakeDraftRepo:
    def __init__(self, drafts: dict[str, Draft]) -> None:
        self._drafts = drafts

    def all_versions(self, run_id: str, uid: str) -> dict[str, int]:
        return {qid: d.version for qid, d in self._drafts.items()}

    def list_all(self, run_id: str, uid: str):
        return list(self._drafts.values())

    def get(self, run_id: str, uid: str, question_id: str):
        return self._drafts.get(question_id)


class _FakeAssignmentRepo:
    def __init__(self, assignments: dict[int, str]) -> None:
        self._assignments = assignments

    def get_assignment(self, *, run_id: str, uid: str, slot_index: int):
        return self._assignments.get(slot_index)


class _FakeGenerationRepo:
    def __init__(self, candidates: dict[str, GenerationCandidate]) -> None:
        self._candidates = candidates

    def get_candidate(self, candidate_id: str):
        return self._candidates.get(candidate_id)

    get_candidate_cached = get_candidate


class _RecordingJudgeService:
    def __init__(self, *_args, **_kwargs) -> None:
        self.final_grade_calls: list[dict] = []
        self.mcq_calls: list[dict] = []
        self.short_answer_calls: list[dict] = []

    def submit_final_grade(self, **kwargs) -> None:
        self.final_grade_calls.append(kwargs)

    def grade_mcq(self, **kwargs) -> None:
        self.mcq_calls.append(kwargs)

    def create_short_answer_review(self, **kwargs) -> None:
        self.short_answer_calls.append(kwargs)


def _candidate(item_type: ItemType, **overrides) -> GenerationCandidate:
    defaults = dict(
        id="cand-1", request_id="req-1", blueprint_id="bp-1", slot_index=0, family_id="fam-1",
        local_candidate_id="local-1", item_type=item_type, objective_ids=["obj-1"], difficulty="medium",
        marks=5.0, estimated_minutes=5, uniqueness_signature="sig-1", public_prompt="Q?",
        status=CandidateStatus.PUBLISHED, created_at=NOW,
    )
    defaults.update(overrides)
    return GenerationCandidate(**defaults)


def _handler(monkeypatch, *, drafts: dict[str, Draft], assignments: dict[int, str], candidates: dict[str, GenerationCandidate]):
    recording = _RecordingJudgeService()
    monkeypatch.setattr(outbox_handler_module, "DraftRepository", lambda db: _FakeDraftRepo(drafts))
    monkeypatch.setattr(outbox_handler_module, "VariantAssignmentRepository", lambda db: _FakeAssignmentRepo(assignments))
    monkeypatch.setattr(outbox_handler_module, "GenerationRepository", lambda db: _FakeGenerationRepo(candidates))
    monkeypatch.setattr(outbox_handler_module, "JudgeService", lambda db, provider: recording)
    handler = GradingTriggerOutboxHandler(db=object(), judge_provider=object(), allowlist=[])
    return handler, recording


def _finalized_entry(uid: str = "u1") -> OutboxEntry:
    return OutboxEntry(id="e1", kind="attempt_finalized", run_id="run-1", payload={"uid": uid, "receipt_id": "r1"}, created_at=NOW)


# ── Dispatch by kind ──────────────────────────────────────────────────────────

def test_non_attempt_finalized_entry_is_ignored(monkeypatch) -> None:
    handler, recording = _handler(monkeypatch, drafts={}, assignments={}, candidates={})
    entry = OutboxEntry(id="e1", kind="run_command", run_id="run-1", created_at=NOW)

    handler.handle(entry)

    assert recording.final_grade_calls == []
    assert recording.mcq_calls == []
    assert recording.short_answer_calls == []


# ── Dispatch by item_type ─────────────────────────────────────────────────────

def test_coding_candidate_dispatches_final_grade(monkeypatch) -> None:
    question_id = "rev-1:slot:0"
    draft = Draft(question_id=question_id, version=1, answer={"language": "python3.11", "source_code": "print(1)"})
    candidate = _candidate(ItemType.CODING)
    handler, recording = _handler(
        monkeypatch, drafts={question_id: draft}, assignments={0: candidate.id}, candidates={candidate.id: candidate}
    )

    handler.handle(_finalized_entry())

    assert len(recording.final_grade_calls) == 1
    call = recording.final_grade_calls[0]
    assert call["language_key"] == "python3.11"
    assert call["source_code"] == "print(1)"
    assert call["candidate_id"] == candidate.id


def test_single_choice_candidate_dispatches_mcq_grading(monkeypatch) -> None:
    question_id = "rev-1:slot:0"
    draft = Draft(question_id=question_id, version=1, answer={"selected": "B"})
    candidate = _candidate(ItemType.SINGLE_CHOICE, private_expected_answer="B")
    handler, recording = _handler(
        monkeypatch, drafts={question_id: draft}, assignments={0: candidate.id}, candidates={candidate.id: candidate}
    )

    handler.handle(_finalized_entry())

    assert len(recording.mcq_calls) == 1
    assert recording.mcq_calls[0]["student_answer"] == "B"
    assert recording.mcq_calls[0]["expected_answer"] == "B"


def test_short_answer_candidate_creates_pending_review(monkeypatch) -> None:
    question_id = "rev-1:slot:0"
    draft = Draft(question_id=question_id, version=1, answer={"text": "my essay response"})
    candidate = _candidate(ItemType.SHORT_ANSWER, marks=8.0)
    handler, recording = _handler(
        monkeypatch, drafts={question_id: draft}, assignments={0: candidate.id}, candidates={candidate.id: candidate}
    )

    handler.handle(_finalized_entry())

    assert len(recording.short_answer_calls) == 1
    assert recording.short_answer_calls[0]["student_answer"] == "my essay response"
    assert recording.short_answer_calls[0]["max_score"] == 8.0


# ── Skip conditions (no exception, no grading call) ──────────────────────────

def test_blank_answer_is_skipped(monkeypatch) -> None:
    question_id = "rev-1:slot:0"
    draft = Draft(question_id=question_id, version=0, answer=None)
    candidate = _candidate(ItemType.CODING)
    handler, recording = _handler(
        monkeypatch, drafts={question_id: draft}, assignments={0: candidate.id}, candidates={candidate.id: candidate}
    )

    handler.handle(_finalized_entry())

    assert recording.final_grade_calls == []


def test_missing_variant_assignment_is_skipped_without_raising(monkeypatch) -> None:
    question_id = "rev-1:slot:0"
    draft = Draft(question_id=question_id, version=1, answer={"language": "python3.11", "source_code": "x"})
    handler, recording = _handler(monkeypatch, drafts={question_id: draft}, assignments={}, candidates={})

    handler.handle(_finalized_entry())  # must not raise

    assert recording.final_grade_calls == []


def test_unapproved_candidate_is_skipped(monkeypatch) -> None:
    question_id = "rev-1:slot:0"
    draft = Draft(question_id=question_id, version=1, answer={"language": "python3.11", "source_code": "x"})
    candidate = _candidate(ItemType.CODING, status=CandidateStatus.DRAFT)
    handler, recording = _handler(
        monkeypatch, drafts={question_id: draft}, assignments={0: candidate.id}, candidates={candidate.id: candidate}
    )

    handler.handle(_finalized_entry())

    assert recording.final_grade_calls == []


def test_multiple_questions_in_one_attempt_are_all_graded(monkeypatch) -> None:
    coding_qid, mcq_qid = "rev-1:slot:0", "rev-1:slot:1"
    coding_candidate = _candidate(ItemType.CODING, id="cand-coding")
    mcq_candidate = _candidate(ItemType.SINGLE_CHOICE, id="cand-mcq", private_expected_answer="C")
    drafts = {
        coding_qid: Draft(question_id=coding_qid, version=1, answer={"language": "python3.11", "source_code": "x"}),
        mcq_qid: Draft(question_id=mcq_qid, version=1, answer={"selected": "C"}),
    }
    handler, recording = _handler(
        monkeypatch, drafts=drafts, assignments={0: coding_candidate.id, 1: mcq_candidate.id},
        candidates={coding_candidate.id: coding_candidate, mcq_candidate.id: mcq_candidate},
    )

    handler.handle(_finalized_entry())

    assert len(recording.final_grade_calls) == 1
    assert len(recording.mcq_calls) == 1
