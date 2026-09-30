"""Gradebook aggregation service tests — fake in-memory repos wired via the
`Service.__new__(Service)` bypass pattern (see
`test_institution_auth_service.py` for the canonical precedent). Exercises
the full per-student-per-question resolution: coding/MCQ/short-answer
classification, pending/blank/defective-question handling, integrity
disposition, and enrolled-but-never-started pagination.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.domain.exams.models import Attempt, AttemptStatus, Draft, ExamRevision, ExamRun
from app.domain.exams.policy import ExamPolicy
from app.domain.gradebook.aggregation_service import GradebookAggregationService
from app.domain.gradebook.models import DefectiveQuestionResolution, IntegrityDisposition, QuestionOutcome
from app.domain.generation.models import CandidateStatus, GenerationCandidate, ItemType
from app.domain.judge.models import GradingResult, McqGradeResult, ShortAnswerReview, ShortAnswerReviewStatus, TestCaseOutcome, TestCaseResult
from app.domain.proctoring.models import Appeal, AppealStatus, ProctoringState

NOW = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
RUN_ID = "run-1"
REVISION_ID = "rev-1"


# ── fakes ─────────────────────────────────────────────────────────────────────

class _FakeRunRepo:
    def __init__(self, run: ExamRun) -> None:
        self._run = run

    def get(self, run_id: str):
        return self._run if run_id == self._run.id else None


class _FakeDefinitionRepo:
    def __init__(self, revision: ExamRevision) -> None:
        self._revision = revision

    def get_revision(self, definition_id: str, revision_id: str):
        return self._revision if revision_id == self._revision.id else None


class _FakeAttemptRepo:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], Attempt] = {}

    def put(self, attempt: Attempt) -> None:
        self.store[(attempt.run_id, attempt.uid)] = attempt

    def get(self, run_id: str, uid: str) -> Optional[Attempt]:
        return self.store.get((run_id, uid))


class _FakeDraftRepo:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str, str], Draft] = {}

    def put(self, run_id: str, uid: str, draft: Draft) -> None:
        self.store[(run_id, uid, draft.question_id)] = draft

    def get(self, run_id: str, uid: str, question_id: str) -> Optional[Draft]:
        return self.store.get((run_id, uid, question_id))


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


class _FakeGradingResultRepo:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], list[GradingResult]] = {}

    def put(self, result: GradingResult) -> None:
        self.store.setdefault((result.run_id, result.uid), []).append(result)

    def list_latest_for_attempt(self, run_id: str, uid: str) -> list[GradingResult]:
        return self.store.get((run_id, uid), [])


class _FakeMcqResultRepo:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], list[McqGradeResult]] = {}

    def put(self, result: McqGradeResult) -> None:
        self.store.setdefault((result.run_id, result.uid), []).append(result)

    def list_for_attempt(self, run_id: str, uid: str) -> list[McqGradeResult]:
        return self.store.get((run_id, uid), [])


class _FakeShortAnswerRepo:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], list[ShortAnswerReview]] = {}

    def put(self, review: ShortAnswerReview) -> None:
        self.store.setdefault((review.run_id, review.uid), []).append(review)

    def list_latest_for_attempt(self, run_id: str, uid: str) -> list[ShortAnswerReview]:
        return self.store.get((run_id, uid), [])


class _FakeProctoringStateRepo:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], ProctoringState] = {}

    def put(self, state: ProctoringState) -> None:
        self.store[(state.run_id, state.uid)] = state

    def get(self, run_id: str, uid: str) -> Optional[ProctoringState]:
        return self.store.get((run_id, uid))


class _FakeAppealRepo:
    def __init__(self) -> None:
        self.items: list[Appeal] = []

    def list_for_run(self, run_id: str) -> list[Appeal]:
        return [a for a in self.items if a.run_id == run_id]


class _FakeDefectiveResolutionRepo:
    def __init__(self) -> None:
        self.items: list[DefectiveQuestionResolution] = []

    def list_for_run(self, run_id: str) -> list[DefectiveQuestionResolution]:
        return [r for r in self.items if r.run_id == run_id]


# ── fixtures ──────────────────────────────────────────────────────────────────

def _revision(slot_count: int = 3) -> ExamRevision:
    return ExamRevision(
        id=REVISION_ID, definition_id="def-1", revision_number=1, title="Midterm",
        published_at=NOW, published_by="fac-1", slot_count=slot_count,
    )


def _run(enrolled_uids: list[str]) -> ExamRun:
    return ExamRun(
        id=RUN_ID, definition_id="def-1", revision_id=REVISION_ID,
        policy=ExamPolicy(duration_minutes=60), enrolled_uids=enrolled_uids, created_at=NOW, updated_at=NOW,
    )


def _candidate(slot_index: int, item_type: ItemType, *, marks: float = 10.0, candidate_id: Optional[str] = None) -> GenerationCandidate:
    cid = candidate_id or f"cand-{slot_index}"
    return GenerationCandidate(
        id=cid, request_id="req-1", blueprint_id="bp-1", slot_index=slot_index, family_id="fam-1",
        local_candidate_id="v1", item_type=item_type, objective_ids=["o1"], difficulty="medium",
        marks=marks, estimated_minutes=10, uniqueness_signature=f"sig-{cid}", public_prompt="Solve this.",
        status=CandidateStatus.PUBLISHED, created_at=NOW,
    )


def _service(
    *, run: ExamRun, revision: ExamRevision, attempts=None, drafts=None, assignments=None, generation=None,
    grading=None, mcq=None, short_answer=None, proctoring_states=None, appeals=None, defective=None,
) -> GradebookAggregationService:
    svc = GradebookAggregationService.__new__(GradebookAggregationService)
    svc.runs = _FakeRunRepo(run)
    svc.definitions = _FakeDefinitionRepo(revision)
    svc.attempts = attempts or _FakeAttemptRepo()
    svc.drafts = drafts or _FakeDraftRepo()
    svc.assignments = assignments or _FakeAssignmentRepo()
    svc.generation = generation or _FakeGenerationRepo()
    svc.grading_results = grading or _FakeGradingResultRepo()
    svc.mcq_results = mcq or _FakeMcqResultRepo()
    svc.short_answer_reviews = short_answer or _FakeShortAnswerRepo()
    svc.proctoring_states = proctoring_states or _FakeProctoringStateRepo()
    svc.proctoring_appeals = appeals or _FakeAppealRepo()
    svc.defective_resolutions = defective or _FakeDefectiveResolutionRepo()
    return svc


def _test_result(outcome: TestCaseOutcome, *, test_id: str = "t1") -> TestCaseResult:
    return TestCaseResult(test_id=test_id, group_name="g", is_hidden=False, weight=1.0, outcome=outcome)


# ── single-student, mixed item types ────────────────────────────────────────

def test_build_student_row_mixed_item_types() -> None:
    run = _run(["u1"])
    revision = _revision(slot_count=3)
    attempts = _FakeAttemptRepo()
    attempts.put(Attempt(run_id=RUN_ID, uid="u1", status=AttemptStatus.FINALIZED, current_slot_index=2))

    assignments = _FakeAssignmentRepo()
    assignments.put(RUN_ID, "u1", 0, "cand-0")
    assignments.put(RUN_ID, "u1", 1, "cand-1")
    assignments.put(RUN_ID, "u1", 2, "cand-2")

    generation = _FakeGenerationRepo()
    generation.put(_candidate(0, ItemType.CODING, marks=10.0))
    generation.put(_candidate(1, ItemType.SINGLE_CHOICE, marks=5.0))
    generation.put(_candidate(2, ItemType.SHORT_ANSWER, marks=8.0))

    drafts = _FakeDraftRepo()
    drafts.put(RUN_ID, "u1", Draft(question_id=f"{REVISION_ID}:slot:0", version=1, answer={"language": "python3.11", "source_code": "print(1)"}))
    drafts.put(RUN_ID, "u1", Draft(question_id=f"{REVISION_ID}:slot:1", version=1, answer={"selected": "A"}))
    drafts.put(RUN_ID, "u1", Draft(question_id=f"{REVISION_ID}:slot:2", version=1, answer={"text": "My answer."}))

    grading = _FakeGradingResultRepo()
    grading.put(GradingResult(
        id="g1", submission_id="s1", candidate_id="cand-0", run_id=RUN_ID, uid="u1",
        question_id=f"{REVISION_ID}:slot:0", test_results=[_test_result(TestCaseOutcome.PASSED), _test_result(TestCaseOutcome.PASSED, test_id="t2")],
        weighted_score=10.0, max_score=10.0, submission_hash="h1", runtime_image="img1", test_suite_version=1, graded_at=NOW,
    ))

    mcq = _FakeMcqResultRepo()
    mcq.put(McqGradeResult(id="m1", run_id=RUN_ID, uid="u1", question_id=f"{REVISION_ID}:slot:1", candidate_id="cand-1", correct=False, graded_at=NOW))

    short_answer = _FakeShortAnswerRepo()
    short_answer.put(ShortAnswerReview(
        id="sa1", run_id=RUN_ID, uid="u1", question_id=f"{REVISION_ID}:slot:2", candidate_id="cand-2",
        student_answer="My answer.", status=ShortAnswerReviewStatus.PENDING, max_score=8.0, created_at=NOW,
    ))

    svc = _service(
        run=run, revision=revision, attempts=attempts, drafts=drafts, assignments=assignments,
        generation=generation, grading=grading, mcq=mcq, short_answer=short_answer,
    )
    row = svc.build_student_row(run, revision, "u1")

    assert row.finalized is True
    assert len(row.question_results) == 3
    coding, single_choice, short = row.question_results
    assert coding.outcome == QuestionOutcome.FULLY_SOLVED
    assert coding.tests_passed == 2 and coding.tests_total == 2
    assert single_choice.outcome == QuestionOutcome.PARTIALLY_SOLVED  # answered but incorrect
    assert single_choice.weighted_score == 0.0
    assert short.outcome == QuestionOutcome.PENDING  # answered, but review not yet REVIEWED
    assert row.raw_score == 10.0  # only the fully-solved coding question counts so far
    assert row.raw_max_score == 23.0
    assert row.integrity_disposition == IntegrityDisposition.CLEAN


def test_blank_question_never_presented_is_blank_not_pending() -> None:
    run = _run(["u1"])
    revision = _revision(slot_count=1)
    attempts = _FakeAttemptRepo()
    attempts.put(Attempt(run_id=RUN_ID, uid="u1", status=AttemptStatus.IN_PROGRESS, current_slot_index=0))
    assignments = _FakeAssignmentRepo()
    assignments.put(RUN_ID, "u1", 0, "cand-0")
    generation = _FakeGenerationRepo()
    generation.put(_candidate(0, ItemType.CODING))

    svc = _service(run=run, revision=revision, attempts=attempts, assignments=assignments, generation=generation)
    row = svc.build_student_row(run, revision, "u1")

    assert row.question_results[0].outcome == QuestionOutcome.BLANK
    assert row.question_results[0].presented is True  # reached the slot, just never answered


def test_never_assigned_variant_is_skipped_not_crashed() -> None:
    run = _run(["u1"])
    revision = _revision(slot_count=1)
    svc = _service(run=run, revision=revision)  # no assignment at all
    row = svc.build_student_row(run, revision, "u1")
    assert row.question_results == []


def test_coding_question_with_pending_infra_failure_is_pending_never_zero() -> None:
    """A submission stuck at INFRA_ERROR (no GradingResult produced yet)
    must never silently read as a zero score — it's PENDING until grading
    actually completes (contract: 'never a silent zero score')."""
    run = _run(["u1"])
    revision = _revision(slot_count=1)
    attempts = _FakeAttemptRepo()
    attempts.put(Attempt(run_id=RUN_ID, uid="u1", status=AttemptStatus.IN_PROGRESS, current_slot_index=0))
    assignments = _FakeAssignmentRepo()
    assignments.put(RUN_ID, "u1", 0, "cand-0")
    generation = _FakeGenerationRepo()
    generation.put(_candidate(0, ItemType.CODING, marks=10.0))
    drafts = _FakeDraftRepo()
    drafts.put(RUN_ID, "u1", Draft(question_id=f"{REVISION_ID}:slot:0", version=1, answer={"language": "python3.11", "source_code": "print(1)"}))
    # No GradingResult exists yet — the submission is stuck retrying against
    # an infra error; nothing in `grading` fakes it as graded.
    svc = _service(run=run, revision=revision, attempts=attempts, assignments=assignments, generation=generation, drafts=drafts)
    row = svc.build_student_row(run, revision, "u1")
    assert row.question_results[0].outcome == QuestionOutcome.PENDING
    assert row.question_results[0].weighted_score == 0.0  # not yet scored, not a zero verdict
    assert row.raw_score == 0.0


def test_regrade_produces_higher_version_and_aggregation_uses_latest() -> None:
    """Two `GradingResult` versions exist for the same question (a regrade
    following an upheld grade appeal) — aggregation must reflect the LATEST
    version, never the stale original."""
    run = _run(["u1"])
    revision = _revision(slot_count=1)
    attempts = _FakeAttemptRepo()
    attempts.put(Attempt(run_id=RUN_ID, uid="u1", status=AttemptStatus.FINALIZED, current_slot_index=0))
    assignments = _FakeAssignmentRepo()
    assignments.put(RUN_ID, "u1", 0, "cand-0")
    generation = _FakeGenerationRepo()
    generation.put(_candidate(0, ItemType.CODING, marks=10.0))
    drafts = _FakeDraftRepo()
    drafts.put(RUN_ID, "u1", Draft(question_id=f"{REVISION_ID}:slot:0", version=1, answer={"language": "python3.11", "source_code": "print(1)"}))
    grading = _FakeGradingResultRepo()
    grading.put(GradingResult(
        id="g1", submission_id="s1", candidate_id="cand-0", run_id=RUN_ID, uid="u1", question_id=f"{REVISION_ID}:slot:0",
        version=1, test_results=[_test_result(TestCaseOutcome.FAILED)], weighted_score=0.0, max_score=10.0,
        submission_hash="h1", runtime_image="img1", test_suite_version=1, graded_at=NOW,
    ))
    grading.put(GradingResult(
        id="g2", submission_id="s1", candidate_id="cand-0", run_id=RUN_ID, uid="u1", question_id=f"{REVISION_ID}:slot:0",
        version=2, test_results=[_test_result(TestCaseOutcome.PASSED)], weighted_score=10.0, max_score=10.0,
        submission_hash="h1", runtime_image="img1", test_suite_version=1, graded_at=NOW,
        graded_by="fac-1", regrade_reason="Hidden test had a bug; corrected and regraded.",
    ))

    svc = _service(run=run, revision=revision, attempts=attempts, assignments=assignments, generation=generation, drafts=drafts, grading=grading)
    row = svc.build_student_row(run, revision, "u1")
    assert row.question_results[0].outcome == QuestionOutcome.FULLY_SOLVED
    assert row.question_results[0].weighted_score == 10.0
    assert row.question_results[0].grading_version == 2


# ── defective-question resolution ───────────────────────────────────────────

def test_defective_question_resolution_overrides_outcome() -> None:
    run = _run(["u1"])
    revision = _revision(slot_count=1)
    attempts = _FakeAttemptRepo()
    attempts.put(Attempt(run_id=RUN_ID, uid="u1", status=AttemptStatus.FINALIZED, current_slot_index=0))
    assignments = _FakeAssignmentRepo()
    assignments.put(RUN_ID, "u1", 0, "cand-0")
    generation = _FakeGenerationRepo()
    generation.put(_candidate(0, ItemType.SINGLE_CHOICE, marks=10.0))
    mcq = _FakeMcqResultRepo()
    mcq.put(McqGradeResult(id="m1", run_id=RUN_ID, uid="u1", question_id=f"{REVISION_ID}:slot:0", candidate_id="cand-0", correct=False, graded_at=NOW))
    drafts = _FakeDraftRepo()
    drafts.put(RUN_ID, "u1", Draft(question_id=f"{REVISION_ID}:slot:0", version=1, answer={"selected": "B"}))
    defective = _FakeDefectiveResolutionRepo()
    defective.items.append(
        DefectiveQuestionResolution(
            id="dq1", run_id=RUN_ID, question_id=f"{REVISION_ID}:slot:0", resolution="full_credit_all",
            reason="Answer key was wrong.", resolved_by="fac-1", resolved_at=NOW,
        )
    )

    svc = _service(run=run, revision=revision, attempts=attempts, assignments=assignments, generation=generation, mcq=mcq, drafts=drafts, defective=defective)
    row = svc.build_student_row(run, revision, "u1")

    assert row.question_results[0].outcome == QuestionOutcome.FULLY_SOLVED
    assert row.question_results[0].weighted_score == 10.0


# ── integrity disposition ───────────────────────────────────────────────────

def test_integrity_disposition_currently_blocked() -> None:
    run = _run(["u1"])
    revision = _revision(slot_count=1)
    attempts = _FakeAttemptRepo()
    attempts.put(Attempt(run_id=RUN_ID, uid="u1", status=AttemptStatus.BLOCKED))
    svc = _service(run=run, revision=revision, attempts=attempts)
    row = svc.build_student_row(run, revision, "u1")
    assert row.integrity_disposition == IntegrityDisposition.BLOCKED_PENDING_REVIEW


def test_integrity_disposition_upheld_when_counted_and_no_overturned_appeal() -> None:
    run = _run(["u1"])
    revision = _revision(slot_count=1)
    attempts = _FakeAttemptRepo()
    attempts.put(Attempt(run_id=RUN_ID, uid="u1", status=AttemptStatus.FINALIZED))
    proctoring_states = _FakeProctoringStateRepo()
    proctoring_states.put(ProctoringState(run_id=RUN_ID, uid="u1", counted_incident_count=1))
    svc = _service(run=run, revision=revision, attempts=attempts, proctoring_states=proctoring_states)
    row = svc.build_student_row(run, revision, "u1")
    assert row.integrity_disposition == IntegrityDisposition.BLOCKED_UPHELD


def test_integrity_disposition_resolved_continued_when_appeal_overturned() -> None:
    run = _run(["u1"])
    revision = _revision(slot_count=1)
    attempts = _FakeAttemptRepo()
    attempts.put(Attempt(run_id=RUN_ID, uid="u1", status=AttemptStatus.FINALIZED))
    proctoring_states = _FakeProctoringStateRepo()
    proctoring_states.put(ProctoringState(run_id=RUN_ID, uid="u1", counted_incident_count=1))
    appeals = _FakeAppealRepo()
    appeals.items.append(Appeal(id="ap1", run_id=RUN_ID, uid="u1", student_statement="It wasn't me.", status=AppealStatus.OVERTURNED, created_at=NOW))
    svc = _service(run=run, revision=revision, attempts=attempts, proctoring_states=proctoring_states, appeals=appeals)
    row = svc.build_student_row(run, revision, "u1")
    assert row.integrity_disposition == IntegrityDisposition.BLOCKED_RESOLVED_CONTINUED


def test_integrity_disposition_clean_when_never_flagged() -> None:
    run = _run(["u1"])
    revision = _revision(slot_count=1)
    attempts = _FakeAttemptRepo()
    attempts.put(Attempt(run_id=RUN_ID, uid="u1", status=AttemptStatus.FINALIZED))
    svc = _service(run=run, revision=revision, attempts=attempts)
    row = svc.build_student_row(run, revision, "u1")
    assert row.integrity_disposition == IntegrityDisposition.CLEAN


# ── pagination over enrolled_uids (including never-started students) ───────

def test_pagination_includes_never_started_students_as_blank_rows() -> None:
    run = _run(["u1", "u2", "u3"])
    revision = _revision(slot_count=1)
    assignments = _FakeAssignmentRepo()
    assignments.put(RUN_ID, "u1", 0, "cand-0")
    generation = _FakeGenerationRepo()
    generation.put(_candidate(0, ItemType.CODING))
    # u2/u3 never admitted — no Attempt doc, no assignment, nothing.
    svc = _service(run=run, revision=revision, assignments=assignments, generation=generation)

    page1, cursor = svc.list_gradebook_page(RUN_ID, limit=2)
    assert [r.uid for r in page1] == ["u1", "u2"]
    assert cursor == "u2"
    assert page1[1].attempt_status == AttemptStatus.NOT_STARTED.value
    assert page1[1].finalized is False

    page2, cursor2 = svc.list_gradebook_page(RUN_ID, limit=2, cursor_uid=cursor)
    assert [r.uid for r in page2] == ["u3"]
    assert cursor2 is None


def test_compute_all_counts_across_run() -> None:
    run = _run(["u1", "u2"])
    revision = _revision(slot_count=1)
    attempts = _FakeAttemptRepo()
    attempts.put(Attempt(run_id=RUN_ID, uid="u1", status=AttemptStatus.FINALIZED, current_slot_index=0))
    attempts.put(Attempt(run_id=RUN_ID, uid="u2", status=AttemptStatus.IN_PROGRESS, current_slot_index=0))
    assignments = _FakeAssignmentRepo()
    assignments.put(RUN_ID, "u1", 0, "cand-0")
    assignments.put(RUN_ID, "u2", 0, "cand-0")
    generation = _FakeGenerationRepo()
    generation.put(_candidate(0, ItemType.CODING, marks=10.0))
    drafts = _FakeDraftRepo()
    drafts.put(RUN_ID, "u1", Draft(question_id=f"{REVISION_ID}:slot:0", version=1, answer={"language": "python3.11", "source_code": "x"}))
    grading = _FakeGradingResultRepo()
    grading.put(GradingResult(
        id="g1", submission_id="s1", candidate_id="cand-0", run_id=RUN_ID, uid="u1", question_id=f"{REVISION_ID}:slot:0",
        test_results=[_test_result(TestCaseOutcome.PASSED)], weighted_score=10.0, max_score=10.0,
        submission_hash="h", runtime_image="img", test_suite_version=1, graded_at=NOW,
    ))

    svc = _service(run=run, revision=revision, attempts=attempts, assignments=assignments, generation=generation, drafts=drafts, grading=grading)
    counts = svc.compute_all_counts(RUN_ID)
    assert len(counts) == 1
    c = counts[0]
    assert c.assigned == 2
    assert c.finalized == 1
    assert c.fully_solved == 1
    assert c.blank == 1  # u2 never drafted an answer
