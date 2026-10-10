"""Gradebook aggregation — the imperative shell that assembles per-student,
per-question gradebook rows from across the exams/generation/judge/
proctoring domains. All CLASSIFICATION logic is pure (`decisions.py`); this
module only reads Firestore and wires the pieces together.
"""

from __future__ import annotations

from typing import Optional

from app.domain.exams.models import RESOLVED_ATTEMPT_STATUSES, Attempt, AttemptStatus, ExamRevision, ExamRun
from app.domain.exams.repository import AttemptRepository, DraftRepository, ExamDefinitionRepository, ExamRunRepository
from app.domain.gradebook.decisions import (
    apply_defective_question_resolution,
    classify_question_outcome,
    coding_failure_category,
    compute_gradebook_counts,
    count_tests_passed,
    decide_integrity_disposition,
)
from app.domain.gradebook.models import (
    GradebookCounts,
    IntegrityDisposition,
    QuestionOutcome,
    StudentGradebookRow,
    StudentQuestionResult,
)
from app.domain.gradebook.repository import DefectiveQuestionResolutionRepository
from app.domain.generation.models import GenerationCandidate, ItemType
from app.domain.generation.repository import GenerationRepository, VariantAssignmentRepository
from app.domain.judge.models import GradingResult, McqGradeResult, ShortAnswerReview, ShortAnswerReviewStatus
from app.domain.judge.repository import GradingResultRepository, McqResultRepository, ShortAnswerReviewRepository
from app.domain.proctoring.models import AppealStatus
from app.domain.proctoring.repository import AppealRepository, ProctoringStateRepository


def _has_nonblank_answer(item_type: ItemType, draft) -> bool:
    """Mirrors the blank check in `judge/outbox_handler.py::_grade_one_question`
    — kept as a small standalone copy rather than a shared import so the
    gradebook domain has no dependency on the outbox wiring module."""
    if draft is None or not draft.answer:
        return False
    if item_type == ItemType.CODING:
        return bool(draft.answer.get("language")) and bool(draft.answer.get("source_code"))
    if item_type in (ItemType.SINGLE_CHOICE, ItemType.MULTIPLE_SELECT):
        return bool(draft.answer.get("selected"))
    if item_type == ItemType.SHORT_ANSWER:
        return bool(draft.answer.get("text"))
    return False


def _build_question_result(
    *,
    uid: str,
    candidate: GenerationCandidate,
    question_id: str,
    slot_index: int,
    presented: bool,
    has_answer: bool,
    grading: Optional[GradingResult],
    mcq: Optional[McqGradeResult],
    short_answer: Optional[ShortAnswerReview],
) -> StudentQuestionResult:
    base = dict(
        uid=uid, question_id=question_id, slot_index=slot_index,
        item_type=candidate.item_type.value, presented=presented,
    )

    if candidate.item_type == ItemType.CODING:
        if grading is not None:
            passed, total = count_tests_passed(grading.test_results)
            outcome = classify_question_outcome(
                has_nonblank_answer=has_answer, grading_available=True,
                weighted_score=grading.weighted_score, max_score=grading.max_score,
            )
            return StudentQuestionResult(
                **base, outcome=outcome, weighted_score=grading.weighted_score, max_score=grading.max_score,
                grading_version=grading.version, tests_passed=passed, tests_total=total,
                failure_category=coding_failure_category(grading.test_results),
                submission_hash=grading.submission_hash, runtime_image=grading.runtime_image,
                test_suite_version=grading.test_suite_version,
            )
        outcome = classify_question_outcome(
            has_nonblank_answer=has_answer, grading_available=False, weighted_score=0.0, max_score=candidate.marks
        )
        return StudentQuestionResult(**base, outcome=outcome, weighted_score=0.0, max_score=candidate.marks)

    if candidate.item_type in (ItemType.SINGLE_CHOICE, ItemType.MULTIPLE_SELECT):
        if mcq is not None:
            weighted = candidate.marks if mcq.correct else 0.0
            outcome = classify_question_outcome(
                has_nonblank_answer=has_answer, grading_available=True, weighted_score=weighted, max_score=candidate.marks
            )
            return StudentQuestionResult(
                **base, outcome=outcome, weighted_score=weighted, max_score=candidate.marks, grading_version=mcq.version
            )
        outcome = classify_question_outcome(
            has_nonblank_answer=has_answer, grading_available=False, weighted_score=0.0, max_score=candidate.marks
        )
        return StudentQuestionResult(**base, outcome=outcome, weighted_score=0.0, max_score=candidate.marks)

    if candidate.item_type == ItemType.SHORT_ANSWER:
        graded = short_answer is not None and short_answer.status == ShortAnswerReviewStatus.REVIEWED
        score = short_answer.score if (graded and short_answer and short_answer.score is not None) else 0.0
        score = min(max(score, 0.0), candidate.marks)  # a score is never shown outside 0..marks
        outcome = classify_question_outcome(
            has_nonblank_answer=has_answer, grading_available=graded, weighted_score=score, max_score=candidate.marks
        )
        return StudentQuestionResult(
            **base, outcome=outcome, weighted_score=score, max_score=candidate.marks,
            grading_version=short_answer.version if short_answer is not None else 1,
        )

    # Defensive fallback for a future item type this module doesn't know
    # about yet — never silently drop the row.
    return StudentQuestionResult(**base, outcome=QuestionOutcome.PENDING, weighted_score=0.0, max_score=candidate.marks)


def _integrity_disposition(
    *, attempt: Attempt, counted_incident_count: int, review_floor: int, block_threshold: int, appeals: list
) -> IntegrityDisposition:
    """See `decide_integrity_disposition` for the exact rule."""
    return decide_integrity_disposition(
        blocked_now=attempt.status == AttemptStatus.BLOCKED, counted_incidents=counted_incident_count,
        block_threshold=block_threshold, review_floor=review_floor,
        appeal_overturned=any(a.status == AppealStatus.OVERTURNED for a in appeals),
    )


class GradebookAggregationService:
    def __init__(self, db) -> None:
        self._db = db
        self.runs = ExamRunRepository(db)
        self.definitions = ExamDefinitionRepository(db)
        self.attempts = AttemptRepository(db)
        self.drafts = DraftRepository(db)
        self.assignments = VariantAssignmentRepository(db)
        self.generation = GenerationRepository(db)
        self.grading_results = GradingResultRepository(db)
        self.mcq_results = McqResultRepository(db)
        self.short_answer_reviews = ShortAnswerReviewRepository(db)
        self.proctoring_states = ProctoringStateRepository(db)
        self.proctoring_appeals = AppealRepository(db)
        self.defective_resolutions = DefectiveQuestionResolutionRepository(db)

    def get_run_and_revision(self, run_id: str) -> tuple[ExamRun, ExamRevision]:
        run = self.runs.get(run_id)
        if run is None:
            raise LookupError(f"exam run {run_id} not found")
        revision = self.definitions.get_revision(run.definition_id, run.revision_id)
        if revision is None:
            raise LookupError(f"revision not found for run {run_id}")
        return run, revision

    def build_student_row(self, run: ExamRun, revision: ExamRevision, uid: str) -> StudentGradebookRow:
        attempt = self.attempts.get(run.id, uid) or Attempt(run_id=run.id, uid=uid, status=AttemptStatus.NOT_STARTED)

        grading_by_q = {r.question_id: r for r in self.grading_results.list_latest_for_attempt(run.id, uid)}
        mcq_by_q = {r.question_id: r for r in self.mcq_results.list_for_attempt(run.id, uid)}
        short_by_q = {r.question_id: r for r in self.short_answer_reviews.list_latest_for_attempt(run.id, uid)}
        # Several resolutions may exist for one question (a later one supersedes an earlier one; both stay on
        # record). The latest by time wins, deterministically, whatever order the store returns them in.
        defective_by_q = {
            d.question_id: d for d in sorted(self.defective_resolutions.list_for_run(run.id), key=lambda r: (r.resolved_at, r.id))
        }

        question_results: list[StudentQuestionResult] = []
        for slot_index in range(revision.slot_count):
            question_id = f"{revision.id}:slot:{slot_index}"
            candidate_id = self.assignments.get_assignment(run_id=run.id, uid=uid, slot_index=slot_index)
            candidate = self.generation.get_candidate(candidate_id) if candidate_id else None
            if candidate is None:
                continue  # never assigned a variant (e.g. generation incomplete) — nothing to grade yet

            draft = self.drafts.get(run.id, uid, question_id)
            has_answer = _has_nonblank_answer(candidate.item_type, draft)
            # "Presented" = the student actually reached the question. A student who never started has not been
            # shown slot 0 just because the stored slot index defaults to 0.
            started = attempt.status not in (AttemptStatus.NOT_STARTED, AttemptStatus.ADMITTED)
            presented = (started and slot_index <= attempt.current_slot_index) or draft is not None

            result = _build_question_result(
                uid=uid, candidate=candidate, question_id=question_id, slot_index=slot_index,
                presented=presented, has_answer=has_answer, grading=grading_by_q.get(question_id),
                mcq=mcq_by_q.get(question_id), short_answer=short_by_q.get(question_id),
            )
            resolution = defective_by_q.get(question_id)
            if resolution is not None:
                result = apply_defective_question_resolution(resolution, result)
            question_results.append(result)

        proctoring_state = self.proctoring_states.get(run.id, uid)
        appeals = [a for a in self.proctoring_appeals.list_for_run(run.id) if a.uid == uid]
        disposition = _integrity_disposition(
            attempt=attempt, counted_incident_count=proctoring_state.counted_incident_count if proctoring_state else 0,
            review_floor=proctoring_state.review_floor if proctoring_state else 0,
            block_threshold=run.policy.warning_threshold, appeals=appeals,
        )

        return StudentGradebookRow(
            uid=uid,
            attempt_status=attempt.status.value,
            finalized=attempt.status in RESOLVED_ATTEMPT_STATUSES,
            raw_score=sum(r.weighted_score for r in question_results),
            raw_max_score=sum(r.max_score for r in question_results),
            integrity_disposition=disposition,
            warning_count=attempt.warning_count,
            question_results=question_results,
        )

    def list_gradebook_page(
        self, run_id: str, *, limit: int, cursor_uid: Optional[str] = None
    ) -> tuple[list[StudentGradebookRow], Optional[str]]:
        """Paginated over `run.enrolled_uids` (not the attempts subcollection)
        so students who never even started still show up as fully-BLANK
        rows — required for an accurate 'assigned' count."""
        run, revision = self.get_run_and_revision(run_id)
        sorted_uids = sorted(run.enrolled_uids)
        if cursor_uid:
            sorted_uids = [u for u in sorted_uids if u > cursor_uid]
        page_uids = sorted_uids[:limit]
        next_cursor = page_uids[-1] if len(sorted_uids) > limit else None
        rows = [self.build_student_row(run, revision, uid) for uid in page_uids]
        return rows, next_cursor

    def list_all_rows(self, run_id: str) -> list[StudentGradebookRow]:
        """Unpaginated — used by export jobs and the run-wide counts
        summary. Bounded by `run.enrolled_uids` (already a single in-memory
        list per ADR 0008), not a separate unbounded query."""
        run, revision = self.get_run_and_revision(run_id)
        return [self.build_student_row(run, revision, uid) for uid in sorted(run.enrolled_uids)]

    def compute_all_counts(self, run_id: str) -> list[GradebookCounts]:
        rows = self.list_all_rows(run_id)
        finalized_uids = frozenset(r.uid for r in rows if r.finalized)
        by_question: dict[str, list[StudentQuestionResult]] = {}
        for row in rows:
            for qr in row.question_results:
                by_question.setdefault(qr.question_id, []).append(qr)
        return [
            compute_gradebook_counts(question_id, results, finalized_uids=finalized_uids)
            for question_id, results in sorted(by_question.items())
        ]
