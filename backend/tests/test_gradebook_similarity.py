"""Similarity review tests: the deterministic fake provider's scripted
pairwise comparison, and `SimilarityService`'s grouping-by-language,
starter-code exclusion, non-coding skip, and match persistence/replace
behavior — via fake in-memory repos."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.domain.generation.models import CandidateStatus, GenerationCandidate, ItemType
from app.domain.gradebook.models import SimilarityMatch
from app.domain.gradebook.similarity_provider import (
    DeterministicFakeSimilarityProvider,
    SimilarityMatchResult,
    SimilaritySubmissionInput,
)
from app.domain.gradebook.similarity_service import SimilarityService
from app.domain.judge.models import CodeSubmission, SubmissionPurpose, SubmissionStatus

NOW = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
RUN_ID = "run-1"
REVISION_ID = "rev-1"
QUESTION_ID = f"{REVISION_ID}:slot:0"


# ── DeterministicFakeSimilarityProvider ──────────────────────────────────────

def test_fake_provider_returns_only_scripted_pairs() -> None:
    provider = DeterministicFakeSimilarityProvider()
    result = SimilarityMatchResult(
        uid_a="u1", submission_id_a="s1", uid_b="u2", submission_id_b="s2", similarity_percent=92.0, matched_spans=[]
    )
    provider.script("s1", "s2", result)

    inputs = [
        SimilaritySubmissionInput(submission_id="s1", uid="u1", language="python3.11", source_code="a"),
        SimilaritySubmissionInput(submission_id="s2", uid="u2", language="python3.11", source_code="b"),
        SimilaritySubmissionInput(submission_id="s3", uid="u3", language="python3.11", source_code="c"),
    ]
    results = provider.compare(inputs)
    assert results == [result]  # s1/s3 and s2/s3 were never scripted — absent, not fabricated


def test_fake_provider_script_order_independent() -> None:
    provider = DeterministicFakeSimilarityProvider()
    result = SimilarityMatchResult(
        uid_a="u1", submission_id_a="s1", uid_b="u2", submission_id_b="s2", similarity_percent=50.0, matched_spans=[]
    )
    provider.script("s2", "s1", result)  # reversed order at scripting time
    inputs = [
        SimilaritySubmissionInput(submission_id="s1", uid="u1", language="py", source_code="a"),
        SimilaritySubmissionInput(submission_id="s2", uid="u2", language="py", source_code="b"),
    ]
    assert provider.compare(inputs) == [result]


# ── SimilarityService ────────────────────────────────────────────────────────

class _FakeAssignmentRepo:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str, int], str] = {}

    def put(self, run_id: str, uid: str, slot_index: int, candidate_id: str) -> None:
        self.store[(run_id, uid, slot_index)] = candidate_id

    def get_assignment(self, *, run_id: str, uid: str, slot_index: int) -> Optional[str]:
        return self.store.get((run_id, uid, slot_index))


class _FakeGenerationRepo:
    def __init__(self) -> None:
        self.store: dict[str, GenerationCandidate] = {}

    def put(self, candidate: GenerationCandidate) -> None:
        self.store[candidate.id] = candidate

    def get_candidate(self, candidate_id: str) -> Optional[GenerationCandidate]:
        return self.store.get(candidate_id)


class _FakeSubmissionRepo:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str, str], CodeSubmission] = {}

    def put(self, submission: CodeSubmission) -> None:
        self.store[(submission.run_id, submission.uid, submission.question_id)] = submission

    def get_latest_final_grade(self, run_id: str, uid: str, question_id: str) -> Optional[CodeSubmission]:
        return self.store.get((run_id, uid, question_id))


class _FakeMatchRepo:
    def __init__(self) -> None:
        self.by_question: dict[tuple[str, str], list[SimilarityMatch]] = {}

    def replace_for_question(self, run_id: str, question_id: str, matches: list[SimilarityMatch]) -> None:
        self.by_question[(run_id, question_id)] = list(matches)

    def list_for_question(self, run_id: str, question_id: str) -> list[SimilarityMatch]:
        return self.by_question.get((run_id, question_id), [])

    def list_for_run(self, run_id: str) -> list[SimilarityMatch]:
        return [m for matches in self.by_question.values() for m in matches if m.run_id == run_id]


def _candidate(*, item_type: ItemType, starter_code: Optional[dict] = None) -> GenerationCandidate:
    return GenerationCandidate(
        id="cand-0", request_id="req-1", blueprint_id="bp-1", slot_index=0, family_id="fam-1",
        local_candidate_id="v1", item_type=item_type, objective_ids=["o1"], difficulty="medium",
        marks=10.0, estimated_minutes=10, uniqueness_signature="sig", public_prompt="Solve.",
        status=CandidateStatus.PUBLISHED, created_at=NOW, starter_code=starter_code or {},
    )


def _submission(uid: str, source_code: str, *, language_key: str = "python3.11") -> CodeSubmission:
    return CodeSubmission(
        id=f"sub-{uid}", candidate_id="cand-0", run_id=RUN_ID, uid=uid, question_id=QUESTION_ID,
        purpose=SubmissionPurpose.FINAL_GRADE, language_key=language_key, source_code=source_code,
        submission_hash=f"hash-{uid}", runtime_image="img", test_suite_version=1,
        status=SubmissionStatus.SUCCEEDED, created_at=NOW, updated_at=NOW,
    )


def _service(*, provider, assignments=None, generation=None, submissions=None) -> SimilarityService:
    svc = SimilarityService.__new__(SimilarityService)
    svc._provider = provider
    svc.matches = _FakeMatchRepo()
    svc._submissions = submissions or _FakeSubmissionRepo()
    svc._generation = generation or _FakeGenerationRepo()
    svc._assignments = assignments or _FakeAssignmentRepo()
    return svc


def test_run_for_question_strips_starter_code_before_comparing() -> None:
    starter = "def solve(n):\n    pass\n"
    assignments = _FakeAssignmentRepo()
    assignments.put(RUN_ID, "u1", 0, "cand-0")
    assignments.put(RUN_ID, "u2", 0, "cand-0")
    generation = _FakeGenerationRepo()
    generation.put(_candidate(item_type=ItemType.CODING, starter_code={"python3.11": starter}))
    submissions = _FakeSubmissionRepo()
    submissions.put(_submission("u1", starter + "    return n * 2\n"))
    submissions.put(_submission("u2", starter + "    return n * 2\n"))

    captured: list[list[SimilaritySubmissionInput]] = []

    class _CapturingProvider:
        def compare(self, inputs):
            captured.append(inputs)
            return []

    svc = _service(provider=_CapturingProvider(), assignments=assignments, generation=generation, submissions=submissions)
    svc.run_for_question(run_id=RUN_ID, question_id=QUESTION_ID, uids=["u1", "u2"])

    assert len(captured) == 1
    for item in captured[0]:
        assert "def solve" not in item.source_code  # boilerplate stripped
        assert item.source_code == "    return n * 2\n"


def test_run_for_question_skips_non_coding_candidates() -> None:
    assignments = _FakeAssignmentRepo()
    assignments.put(RUN_ID, "u1", 0, "cand-0")
    generation = _FakeGenerationRepo()
    generation.put(_candidate(item_type=ItemType.SHORT_ANSWER))
    svc = _service(provider=DeterministicFakeSimilarityProvider(), assignments=assignments, generation=generation)
    matches = svc.run_for_question(run_id=RUN_ID, question_id=QUESTION_ID, uids=["u1"])
    assert matches == []


def test_run_for_question_skips_students_with_no_final_submission() -> None:
    assignments = _FakeAssignmentRepo()
    assignments.put(RUN_ID, "u1", 0, "cand-0")
    generation = _FakeGenerationRepo()
    generation.put(_candidate(item_type=ItemType.CODING))
    # No submission for u1 — never submitted / still pending grading.
    svc = _service(provider=DeterministicFakeSimilarityProvider(), assignments=assignments, generation=generation)
    matches = svc.run_for_question(run_id=RUN_ID, question_id=QUESTION_ID, uids=["u1"])
    assert matches == []


def test_run_for_question_persists_and_replaces_prior_matches() -> None:
    assignments = _FakeAssignmentRepo()
    assignments.put(RUN_ID, "u1", 0, "cand-0")
    assignments.put(RUN_ID, "u2", 0, "cand-0")
    generation = _FakeGenerationRepo()
    generation.put(_candidate(item_type=ItemType.CODING))
    submissions = _FakeSubmissionRepo()
    submissions.put(_submission("u1", "print(1)"))
    submissions.put(_submission("u2", "print(1)"))

    provider = DeterministicFakeSimilarityProvider()
    provider.script("sub-u1", "sub-u2", SimilarityMatchResult(
        uid_a="u1", submission_id_a="sub-u1", uid_b="u2", submission_id_b="sub-u2",
        similarity_percent=97.5, matched_spans=[{"start_a": 0, "end_a": 8}],
    ))
    svc = _service(provider=provider, assignments=assignments, generation=generation, submissions=submissions)

    first_run = svc.run_for_question(run_id=RUN_ID, question_id=QUESTION_ID, uids=["u1", "u2"])
    assert len(first_run) == 1
    assert first_run[0].similarity_percent == 97.5
    assert svc.list_matches(RUN_ID, QUESTION_ID) == first_run

    # Re-running with nothing scripted this time supersedes the old matches.
    provider._scripted.clear()
    second_run = svc.run_for_question(run_id=RUN_ID, question_id=QUESTION_ID, uids=["u1", "u2"])
    assert second_run == []
    assert svc.list_matches(RUN_ID, QUESTION_ID) == []


def test_run_for_question_needs_at_least_two_submissions_per_language() -> None:
    assignments = _FakeAssignmentRepo()
    assignments.put(RUN_ID, "u1", 0, "cand-0")
    generation = _FakeGenerationRepo()
    generation.put(_candidate(item_type=ItemType.CODING))
    submissions = _FakeSubmissionRepo()
    submissions.put(_submission("u1", "print(1)"))

    class _FailIfCalledProvider:
        def compare(self, inputs):
            raise AssertionError("should never be called with only one submission")

    svc = _service(provider=_FailIfCalledProvider(), assignments=assignments, generation=generation, submissions=submissions)
    matches = svc.run_for_question(run_id=RUN_ID, question_id=QUESTION_ID, uids=["u1"])
    assert matches == []
