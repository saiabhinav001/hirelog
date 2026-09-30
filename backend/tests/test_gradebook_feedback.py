"""Learning feedback tests: the pure template-based generator (strengths/
gaps/practice-steps/source-anchors/missing-material across every outcome,
with PENDING silently excluded) and `FeedbackService`'s release-gating and
anchor resolution — via fake in-memory repos.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest

from app.domain.gradebook.errors import ResultsNotReleasedError
from app.domain.gradebook.feedback_service import FeedbackService, build_learning_feedback
from app.domain.gradebook.models import FailureCategory, IntegrityDisposition, QuestionOutcome, ReleaseRecord, StudentGradebookRow, StudentQuestionResult

NOW = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
RUN_ID = "run-1"


def _qr(slot_index: int, outcome: QuestionOutcome, **overrides) -> StudentQuestionResult:
    defaults = dict(
        uid="u1", question_id=f"rev-1:slot:{slot_index}", slot_index=slot_index, item_type="coding",
        presented=True, outcome=outcome, weighted_score=0.0, max_score=10.0,
    )
    defaults.update(overrides)
    return StudentQuestionResult(**defaults)


def _row(question_results: list[StudentQuestionResult]) -> StudentGradebookRow:
    return StudentGradebookRow(
        uid="u1", attempt_status="finalized", finalized=True,
        raw_score=sum(q.weighted_score for q in question_results), raw_max_score=sum(q.max_score for q in question_results),
        integrity_disposition=IntegrityDisposition.CLEAN, warning_count=0, question_results=question_results,
    )


# ── build_learning_feedback (pure) ──────────────────────────────────────────

def test_fully_solved_question_becomes_a_strength() -> None:
    row = _row([_qr(0, QuestionOutcome.FULLY_SOLVED, weighted_score=10.0, max_score=10.0)])
    feedback = build_learning_feedback(row, run_id=RUN_ID, supporting_anchors_by_question={})
    assert len(feedback.strengths) == 1
    assert "Question 1" in feedback.strengths[0]
    assert feedback.gaps == []


def test_pending_question_is_silently_excluded() -> None:
    row = _row([_qr(0, QuestionOutcome.PENDING)])
    feedback = build_learning_feedback(row, run_id=RUN_ID, supporting_anchors_by_question={})
    assert feedback.strengths == [] and feedback.gaps == [] and feedback.missing_material == []


def test_partially_solved_coding_question_adds_gap_and_failure_hint() -> None:
    row = _row([_qr(0, QuestionOutcome.PARTIALLY_SOLVED, weighted_score=4.0, failure_category=FailureCategory.TIME_LIMIT_EXCEEDED)])
    feedback = build_learning_feedback(row, run_id=RUN_ID, supporting_anchors_by_question={"rev-1:slot:0": ["src-1:anchor-1"]})
    assert len(feedback.gaps) == 1
    assert any("too slow" in hint for hint in feedback.practice_steps)
    assert feedback.source_anchors == ["src-1:anchor-1"]
    assert feedback.missing_material == []


def test_blank_question_adds_gap_and_missing_material_when_no_anchors() -> None:
    row = _row([_qr(0, QuestionOutcome.BLANK)])
    feedback = build_learning_feedback(row, run_id=RUN_ID, supporting_anchors_by_question={})
    assert "not attempted" in feedback.gaps[0]
    assert len(feedback.missing_material) == 1
    assert feedback.source_anchors == []


def test_run_id_and_uid_are_stamped_onto_feedback() -> None:
    row = _row([_qr(0, QuestionOutcome.FULLY_SOLVED, weighted_score=10.0, max_score=10.0)])
    feedback = build_learning_feedback(row, run_id=RUN_ID, supporting_anchors_by_question={})
    assert feedback.run_id == RUN_ID
    assert feedback.uid == "u1"


def test_source_anchors_are_deduplicated_and_sorted() -> None:
    row = _row([
        _qr(0, QuestionOutcome.BLANK),
        _qr(1, QuestionOutcome.PARTIALLY_SOLVED, weighted_score=2.0),
    ])
    anchors = {"rev-1:slot:0": ["src-b:a1", "src-a:a1"], "rev-1:slot:1": ["src-a:a1"]}
    feedback = build_learning_feedback(row, run_id=RUN_ID, supporting_anchors_by_question=anchors)
    assert feedback.source_anchors == ["src-a:a1", "src-b:a1"]


# ── FeedbackService ──────────────────────────────────────────────────────────

class _FakeReleaseRepo:
    def __init__(self, released: bool) -> None:
        self._released = released

    def latest_for_run(self, run_id: str) -> Optional[ReleaseRecord]:
        return ReleaseRecord(id="r1", run_id=run_id, version=1, released_by="fac1", released_at=NOW) if self._released else None


class _FakeAggregation:
    def __init__(self, row: StudentGradebookRow) -> None:
        self._row = row
        self.assignments = _FakeAssignments()
        self.generation = _FakeGeneration()

    def get_run_and_revision(self, run_id: str):
        return object(), object()

    def build_student_row(self, run, revision, uid) -> StudentGradebookRow:
        return self._row


class _FakeAssignments:
    def get_assignment(self, *, run_id, uid, slot_index):
        return f"cand-{slot_index}"


class _FakeGeneration:
    def get_candidate(self, candidate_id: str):
        from app.domain.generation.models import CandidateStatus, GenerationCandidate, ItemType, SourceAnchorRef

        return GenerationCandidate(
            id=candidate_id, request_id="req", blueprint_id="bp", slot_index=0, family_id="f",
            local_candidate_id="v1", item_type=ItemType.CODING, objective_ids=["o1"], difficulty="easy",
            marks=10.0, estimated_minutes=10, uniqueness_signature="sig", public_prompt="p",
            status=CandidateStatus.PUBLISHED, created_at=NOW,
            supporting_anchors=[SourceAnchorRef(source_id="src-1", source_version=1, anchor_id="a1")],
        )


def _feedback_service(*, released: bool, row: StudentGradebookRow) -> FeedbackService:
    svc = FeedbackService.__new__(FeedbackService)
    svc._releases = _FakeReleaseRepo(released)
    svc._aggregation = _FakeAggregation(row)
    return svc


def test_feedback_service_raises_when_not_released() -> None:
    row = _row([_qr(0, QuestionOutcome.FULLY_SOLVED, weighted_score=10.0, max_score=10.0)])
    svc = _feedback_service(released=False, row=row)
    with pytest.raises(ResultsNotReleasedError):
        svc.build_feedback_for_student(RUN_ID, "u1")


def test_feedback_service_resolves_anchors_when_released() -> None:
    row = _row([_qr(0, QuestionOutcome.BLANK)])
    svc = _feedback_service(released=True, row=row)
    feedback = svc.build_feedback_for_student(RUN_ID, "u1")
    assert feedback.source_anchors == ["src-1:a1"]
