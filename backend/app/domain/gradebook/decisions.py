"""Pure, Firestore-free gradebook/similarity/export decision functions.
Zero mocking needed to test any of this — every function takes plain values
and returns plain values or raises a domain error.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.gradebook.models import (
    DefectiveQuestionResolution,
    FailureCategory,
    GradeAppeal,
    GradeAppealStatus,
    GradebookCounts,
    IntegrityDisposition,
    QuestionOutcome,
    ReleaseRecord,
    StudentGradebookRow,
    StudentQuestionResult,
)
from app.domain.judge.models import TestCaseOutcome, TestCaseResult

# ── per-question outcome classification ─────────────────────────────────────

# Worst-first — a coding answer can fail several tests in different ways;
# the gradebook surfaces the single most-severe outcome seen so faculty can
# triage without opening every submission.
_FAILURE_SEVERITY: tuple[TestCaseOutcome, ...] = (
    TestCaseOutcome.COMPILE_ERROR,
    TestCaseOutcome.RUNTIME_ERROR,
    TestCaseOutcome.TIME_LIMIT_EXCEEDED,
    TestCaseOutcome.MEMORY_LIMIT_EXCEEDED,
    TestCaseOutcome.OUTPUT_LIMIT_EXCEEDED,
    TestCaseOutcome.FAILED,
)
_TO_FAILURE_CATEGORY = {
    TestCaseOutcome.COMPILE_ERROR: FailureCategory.COMPILE_ERROR,
    TestCaseOutcome.RUNTIME_ERROR: FailureCategory.RUNTIME_ERROR,
    TestCaseOutcome.TIME_LIMIT_EXCEEDED: FailureCategory.TIME_LIMIT_EXCEEDED,
    TestCaseOutcome.MEMORY_LIMIT_EXCEEDED: FailureCategory.MEMORY_LIMIT_EXCEEDED,
    TestCaseOutcome.OUTPUT_LIMIT_EXCEEDED: FailureCategory.OUTPUT_LIMIT_EXCEEDED,
    TestCaseOutcome.FAILED: FailureCategory.WRONG_ANSWER,
}


def classify_question_outcome(
    *, has_nonblank_answer: bool, grading_available: bool, weighted_score: float, max_score: float
) -> QuestionOutcome:
    """The four-state classification named by the prompt. `grading_available`
    means "a completed grading record exists" — for coding this is a
    `GradingResult`, for MCQ an `McqGradeResult`, for short answer a
    `ShortAnswerReview` with status REVIEWED. Its absence (stuck submission,
    infra error, unreviewed short answer) is always PENDING, never a silent
    zero (contract: 'never a silent zero score')."""
    if not has_nonblank_answer:
        return QuestionOutcome.BLANK
    if not grading_available:
        return QuestionOutcome.PENDING
    if max_score <= 0:
        return QuestionOutcome.FULLY_SOLVED
    if weighted_score >= max_score:
        return QuestionOutcome.FULLY_SOLVED
    return QuestionOutcome.PARTIALLY_SOLVED


def coding_failure_category(test_results: list[TestCaseResult]) -> FailureCategory:
    """Worst outcome across all failed tests; NONE if every test passed."""
    failed = {t.outcome for t in test_results if t.outcome != TestCaseOutcome.PASSED}
    if not failed:
        return FailureCategory.NONE
    for outcome in _FAILURE_SEVERITY:
        if outcome in failed:
            return _TO_FAILURE_CATEGORY[outcome]
    # INFRA_ERROR-only (no student-attributable failure) — not a code
    # failure at all; surfaced distinctly so faculty don't blame the student.
    return FailureCategory.NONE


def count_tests_passed(test_results: list[TestCaseResult]) -> tuple[int, int]:
    """NOTE: not named `tests_*` — pytest's default `python_functions = test_*`
    would otherwise try to collect this as a test case wherever it's imported."""
    passed = sum(1 for t in test_results if t.outcome == TestCaseOutcome.PASSED)
    return passed, len(test_results)


# ── aggregate counts ─────────────────────────────────────────────────────────


def compute_gradebook_counts(
    question_id: str, results: list[StudentQuestionResult], *, finalized_uids: frozenset[str]
) -> GradebookCounts:
    """One question's counts across every assigned student. `finalized_uids`
    is attempt-level (an attempt is finalized or it isn't — not per
    question), cross-referenced here per the prompt's 'finalized' count."""
    counts = GradebookCounts(question_id=question_id)
    for row in results:
        counts.assigned += 1
        if row.presented:
            counts.presented += 1
        if row.uid in finalized_uids:
            counts.finalized += 1
        if row.outcome != QuestionOutcome.BLANK:
            counts.nonblank_attempted += 1
        if row.outcome == QuestionOutcome.FULLY_SOLVED:
            counts.fully_solved += 1
        elif row.outcome == QuestionOutcome.PARTIALLY_SOLVED:
            counts.partially_solved += 1
        elif row.outcome == QuestionOutcome.BLANK:
            counts.blank += 1
        elif row.outcome == QuestionOutcome.PENDING:
            counts.pending += 1
    return counts


# ── defective-question resolution ───────────────────────────────────────────


def apply_defective_question_resolution(
    resolution: DefectiveQuestionResolution, result: StudentQuestionResult
) -> StudentQuestionResult:
    """A read-time transformation only — the original `GradingResult` /
    `McqGradeResult` / `ShortAnswerReview` is never touched (contract:
    'never overwriting original submissions'). Applied uniformly to every
    assigned student's row for this question."""
    if resolution.resolution == "full_credit_all":
        return result.model_copy(
            update={
                "weighted_score": result.max_score,
                "outcome": QuestionOutcome.FULLY_SOLVED if result.max_score > 0 else result.outcome,
            }
        )
    if resolution.resolution == "exclude_from_total":
        # Zeroing max_score too means this question contributes nothing to
        # either side of the fraction — a true exclusion, not a zero score.
        return result.model_copy(update={"weighted_score": 0.0, "max_score": 0.0})
    return result


# ── release versioning ───────────────────────────────────────────────────────


def decide_next_release_version(previous_releases: list[ReleaseRecord]) -> int:
    """v1 the first time; v2, v3, ... after each subsequent re-release
    (e.g. following a regrade) — never reuses or overwrites a prior version
    (contract: 'corrected results with auditable lineage')."""
    if not previous_releases:
        return 1
    return max(r.version for r in previous_releases) + 1


# ── grade appeals ────────────────────────────────────────────────────────────


def decide_resolve_grade_appeal(
    appeal: GradeAppeal, *, uphold: bool, resolved_by: str, resolution_notes: str, now: datetime
) -> GradeAppeal:
    """Mirrors `AppealsService.resolve_appeal` (Prompt 8): resolving a
    PENDING appeal for the first time does not bump `version`; reversing an
    already-resolved appeal does (auditable lineage for the rare
    re-reversal)."""
    is_reversal = appeal.status != GradeAppealStatus.PENDING
    new_status = GradeAppealStatus.UPHELD if uphold else GradeAppealStatus.CORRECTED
    return appeal.model_copy(
        update={
            "status": new_status,
            "resolved_by": resolved_by,
            "resolved_at": now,
            "resolution_notes": resolution_notes,
            "version": appeal.version + 1 if is_reversal else appeal.version,
        }
    )


# ── student-facing view ──────────────────────────────────────────────────────


def student_row_view(row: StudentGradebookRow) -> StudentGradebookRow:
    """What a student may see of their own released row: scores, outcome and
    the coding failure category, without the internal grading provenance
    (submission hash, runtime image, hidden test-suite version)."""
    return row.model_copy(update={
        "question_results": [
            q.model_copy(update={"submission_hash": None, "runtime_image": None, "test_suite_version": None})
            for q in row.question_results
        ],
    })


# ── integrity disposition ────────────────────────────────────────────────────


def decide_integrity_disposition(
    *, blocked_now: bool, counted_incidents: int, block_threshold: int, review_floor: int, appeal_overturned: bool
) -> IntegrityDisposition:
    """Separate from the academic score and never an automatic penalty. A
    student reaches a block only when the counted incidents reach the run's
    threshold, so fewer counted incidents than that is CLEAN here (the
    incidents themselves stay visible in the ledger and `warning_count`).
    `review_floor > 0` means a reviewer explicitly allowed the attempt to
    continue after a block."""
    if blocked_now:
        return IntegrityDisposition.BLOCKED_PENDING_REVIEW
    if appeal_overturned or review_floor > 0:
        return IntegrityDisposition.BLOCKED_RESOLVED_CONTINUED
    if counted_incidents > 0 and counted_incidents >= block_threshold:
        return IntegrityDisposition.BLOCKED_UPHELD
    return IntegrityDisposition.CLEAN


# ── CSV export safety ────────────────────────────────────────────────────────

# OWASP CSV-injection guidance: a cell whose first character is one of these
# is interpreted as a formula by Excel/LibreOffice/Google Sheets when the
# file is opened. Prefixing with a single quote neutralizes it while leaving
# the visible value unchanged in every spreadsheet application.
_FORMULA_TRIGGER_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")
# Characters some spreadsheet importers skip before deciding a cell is a formula.
_IGNORABLE_LEADING = " \t\r\n\x00\ufeff\u00a0\u200b"


def sanitize_csv_cell(value: str) -> str:
    """Neutralizes a spreadsheet formula: the OWASP trigger set (`= + - @`,
    TAB, CR) plus LF and the full-width look-alikes, also when hidden behind
    leading whitespace / zero-width characters that an importer may skip."""
    if not value:
        return value
    if value[0] in _FORMULA_TRIGGER_PREFIXES:
        return "'" + value
    stripped = value.lstrip(_IGNORABLE_LEADING)
    if stripped and stripped[0] in _FORMULA_TRIGGER_PREFIXES:
        return "'" + value
    return value


# ── similarity review ────────────────────────────────────────────────────────


def strip_starter_code(source_code: str, starter_code: str) -> str:
    """Removes every exact occurrence of the shared instructor boilerplate
    so identical starter code across every student is never itself counted
    as similarity (contract: 'excluding instructor starter code').
    Conservative by design — only an exact substring match is stripped,
    never a fuzzy/partial one, so legitimate student code is never
    accidentally removed."""
    if not starter_code:
        return source_code
    return source_code.replace(starter_code, "")
