"""Pure gradebook decision tests: outcome classification, coding failure
category, aggregate counts, defective-question resolution, release
versioning, grade-appeal resolution, and CSV-injection-safe export cells.
Zero mocking — everything here is a plain-value pure function.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.gradebook.decisions import (
    apply_defective_question_resolution,
    classify_question_outcome,
    coding_failure_category,
    compute_gradebook_counts,
    count_tests_passed,
    decide_next_release_version,
    decide_resolve_grade_appeal,
    sanitize_csv_cell,
    strip_starter_code,
)
from app.domain.gradebook.models import (
    DefectiveQuestionResolution,
    FailureCategory,
    GradeAppeal,
    GradeAppealStatus,
    QuestionOutcome,
    ReleaseRecord,
    StudentQuestionResult,
)
from app.domain.judge.models import TestCaseOutcome, TestCaseResult

NOW = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)


def _test_result(outcome: TestCaseOutcome, *, test_id: str = "t1") -> TestCaseResult:
    return TestCaseResult(test_id=test_id, group_name="g", is_hidden=False, weight=1.0, outcome=outcome)


def _row(uid: str, question_id: str = "q1", **overrides) -> StudentQuestionResult:
    defaults = dict(
        uid=uid, question_id=question_id, slot_index=0, item_type="coding",
        presented=True, outcome=QuestionOutcome.BLANK,
    )
    defaults.update(overrides)
    return StudentQuestionResult(**defaults)


# ── classify_question_outcome ────────────────────────────────────────────────

def test_blank_when_no_nonblank_answer() -> None:
    outcome = classify_question_outcome(has_nonblank_answer=False, grading_available=True, weighted_score=0, max_score=10)
    assert outcome == QuestionOutcome.BLANK


def test_blank_takes_priority_over_grading_availability() -> None:
    # Even if a grading record somehow exists (e.g. leftover from a prior
    # variant), a blank answer is always BLANK, never PENDING/scored.
    outcome = classify_question_outcome(has_nonblank_answer=False, grading_available=False, weighted_score=0, max_score=10)
    assert outcome == QuestionOutcome.BLANK


def test_pending_when_nonblank_but_not_yet_graded() -> None:
    outcome = classify_question_outcome(has_nonblank_answer=True, grading_available=False, weighted_score=0, max_score=10)
    assert outcome == QuestionOutcome.PENDING


def test_fully_solved_when_score_meets_max() -> None:
    outcome = classify_question_outcome(has_nonblank_answer=True, grading_available=True, weighted_score=10, max_score=10)
    assert outcome == QuestionOutcome.FULLY_SOLVED


def test_fully_solved_when_score_exceeds_max_defensively() -> None:
    outcome = classify_question_outcome(has_nonblank_answer=True, grading_available=True, weighted_score=10.5, max_score=10)
    assert outcome == QuestionOutcome.FULLY_SOLVED


def test_partially_solved_when_score_below_max_including_zero() -> None:
    zero = classify_question_outcome(has_nonblank_answer=True, grading_available=True, weighted_score=0, max_score=10)
    partial = classify_question_outcome(has_nonblank_answer=True, grading_available=True, weighted_score=4, max_score=10)
    assert zero == QuestionOutcome.PARTIALLY_SOLVED
    assert partial == QuestionOutcome.PARTIALLY_SOLVED


def test_zero_max_score_graded_nonblank_is_fully_solved() -> None:
    # Defensive edge case (shouldn't happen — marks are validated > 0 at
    # authoring time) but must never divide by zero or misclassify.
    outcome = classify_question_outcome(has_nonblank_answer=True, grading_available=True, weighted_score=0, max_score=0)
    assert outcome == QuestionOutcome.FULLY_SOLVED


# ── coding_failure_category / tests_passed_total ────────────────────────────

def test_failure_category_none_when_all_passed() -> None:
    results = [_test_result(TestCaseOutcome.PASSED), _test_result(TestCaseOutcome.PASSED, test_id="t2")]
    assert coding_failure_category(results) == FailureCategory.NONE
    assert count_tests_passed(results) == (2, 2)


def test_failure_category_picks_worst_by_severity() -> None:
    results = [
        _test_result(TestCaseOutcome.FAILED, test_id="t1"),
        _test_result(TestCaseOutcome.COMPILE_ERROR, test_id="t2"),
        _test_result(TestCaseOutcome.PASSED, test_id="t3"),
    ]
    assert coding_failure_category(results) == FailureCategory.COMPILE_ERROR
    assert count_tests_passed(results) == (1, 3)


def test_failure_category_infra_error_only_is_none() -> None:
    # An infra-side failure is never attributed to the student as a "wrong
    # answer" category.
    results = [_test_result(TestCaseOutcome.INFRA_ERROR), _test_result(TestCaseOutcome.PASSED, test_id="t2")]
    assert coding_failure_category(results) == FailureCategory.NONE


# ── compute_gradebook_counts ─────────────────────────────────────────────────

def test_counts_across_mixed_outcomes() -> None:
    rows = [
        _row("u1", presented=True, outcome=QuestionOutcome.FULLY_SOLVED),
        _row("u2", presented=True, outcome=QuestionOutcome.PARTIALLY_SOLVED),
        _row("u3", presented=True, outcome=QuestionOutcome.BLANK),
        _row("u4", presented=True, outcome=QuestionOutcome.PENDING),
        _row("u5", presented=False, outcome=QuestionOutcome.BLANK),
    ]
    counts = compute_gradebook_counts("q1", rows, finalized_uids=frozenset({"u1", "u2", "u3"}))
    assert counts.assigned == 5
    assert counts.presented == 4
    assert counts.finalized == 3
    assert counts.nonblank_attempted == 3  # fully + partial + pending, not the two blanks
    assert counts.fully_solved == 1
    assert counts.partially_solved == 1
    assert counts.blank == 2
    assert counts.pending == 1


def test_counts_all_zero_for_empty_run() -> None:
    counts = compute_gradebook_counts("q1", [], finalized_uids=frozenset())
    assert counts.assigned == 0
    assert counts.fully_solved == 0


# ── defective-question resolution ───────────────────────────────────────────

def test_full_credit_all_resolution_maxes_score() -> None:
    resolution = DefectiveQuestionResolution(
        id="r1", run_id="run1", question_id="q1", resolution="full_credit_all",
        reason="Ambiguous prompt.", resolved_by="fac1", resolved_at=NOW,
    )
    row = _row("u1", weighted_score=2.0, max_score=10.0, outcome=QuestionOutcome.PARTIALLY_SOLVED)
    updated = apply_defective_question_resolution(resolution, row)
    assert updated.weighted_score == 10.0
    assert updated.outcome == QuestionOutcome.FULLY_SOLVED
    # Original row object is untouched (pure, immutable).
    assert row.weighted_score == 2.0


def test_exclude_from_total_resolution_zeroes_both_sides() -> None:
    resolution = DefectiveQuestionResolution(
        id="r1", run_id="run1", question_id="q1", resolution="exclude_from_total",
        reason="Unsolvable due to a typo.", resolved_by="fac1", resolved_at=NOW,
    )
    row = _row("u1", weighted_score=3.0, max_score=10.0, outcome=QuestionOutcome.PARTIALLY_SOLVED)
    updated = apply_defective_question_resolution(resolution, row)
    assert updated.weighted_score == 0.0
    assert updated.max_score == 0.0


# ── release versioning ───────────────────────────────────────────────────────

def test_first_release_is_version_one() -> None:
    assert decide_next_release_version([]) == 1


def test_subsequent_release_increments_from_max_existing() -> None:
    releases = [
        ReleaseRecord(id="r1", run_id="run1", version=1, released_by="fac1", released_at=NOW),
        ReleaseRecord(id="r2", run_id="run1", version=2, released_by="fac1", released_at=NOW),
    ]
    assert decide_next_release_version(releases) == 3


# ── grade appeals ────────────────────────────────────────────────────────────

def _appeal(**overrides) -> GradeAppeal:
    defaults = dict(id="a1", run_id="run1", uid="u1", question_id="q1", student_statement="I deserve more credit.", created_at=NOW)
    defaults.update(overrides)
    return GradeAppeal(**defaults)


def test_first_resolution_does_not_bump_version() -> None:
    appeal = _appeal()
    resolved = decide_resolve_grade_appeal(appeal, uphold=True, resolved_by="fac1", resolution_notes="Correct as graded.", now=NOW)
    assert resolved.status == GradeAppealStatus.UPHELD
    assert resolved.version == 1


def test_reversal_of_resolved_appeal_bumps_version() -> None:
    appeal = _appeal(status=GradeAppealStatus.UPHELD, resolved_by="fac1", resolved_at=NOW, version=1)
    reversed_ = decide_resolve_grade_appeal(appeal, uphold=False, resolved_by="fac2", resolution_notes="On further review, corrected.", now=NOW)
    assert reversed_.status == GradeAppealStatus.CORRECTED
    assert reversed_.version == 2


# ── CSV export safety ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["=SUM(A1:A9)", "+1+1", "-2+3", "@cmd"])
def test_formula_trigger_prefixes_are_neutralized(raw: str) -> None:
    sanitized = sanitize_csv_cell(raw)
    assert sanitized == f"'{raw}"
    assert sanitized[0] == "'"


def test_ordinary_values_are_untouched() -> None:
    assert sanitize_csv_cell("Jane Doe") == "Jane Doe"
    assert sanitize_csv_cell("85.5") == "85.5"
    assert sanitize_csv_cell("") == ""


def test_negative_number_like_value_is_still_neutralized_defensively() -> None:
    # A plain negative number ("-5") is indistinguishable from a formula
    # prefix at the character level, so it is conservatively neutralized too
    # — correct rendering in spreadsheet apps (leading `'` is invisible),
    # never a security gap.
    assert sanitize_csv_cell("-5") == "'-5"


# ── similarity: starter-code exclusion ──────────────────────────────────────────────

def test_strip_starter_code_removes_exact_occurrence() -> None:
    starter = "def solve(n):\n    pass\n"
    source = starter + "    return n * 2\n"
    assert strip_starter_code(source, starter) == "    return n * 2\n"


def test_strip_starter_code_no_starter_code_is_passthrough() -> None:
    assert strip_starter_code("print('hi')", "") == "print('hi')"


def test_strip_starter_code_absent_in_source_changes_nothing() -> None:
    assert strip_starter_code("print('hi')", "def solve(): pass") == "print('hi')"
