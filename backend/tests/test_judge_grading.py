"""Pure grading-logic tests: Judge0 status classification (code failure vs
infra error), weighted scoring, MCQ exact match, regrade versioning, and the
hidden-data-leakage guard on student-safe DTOs.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.judge.grading import (
    aggregate_weighted_score,
    classify_status,
    create_regrade,
    grade_mcq_exact_match,
    is_infra_error,
    new_grading_result,
    public_test_result_dto,
    sealed_grade_summary_dto,
)
from app.domain.judge.models import GradingResult, TestCaseOutcome, TestCaseResult
from app.domain.judge.provider import (
    JudgeRawResult,
    STATUS_ACCEPTED,
    STATUS_COMPILE_ERROR,
    STATUS_EXEC_FORMAT_ERROR,
    STATUS_INTERNAL_ERROR,
    STATUS_OUTPUT_LIMIT_EXCEEDED,
    STATUS_RUNTIME_ERROR_MAX,
    STATUS_RUNTIME_ERROR_MIN,
    STATUS_TIME_LIMIT_EXCEEDED,
    STATUS_WRONG_ANSWER,
)

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _raw(status_id: int, **overrides) -> JudgeRawResult:
    defaults = dict(token="tok-1", status_id=status_id, status_description="x")
    defaults.update(overrides)
    return JudgeRawResult(**defaults)


# ── classify_status ──────────────────────────────────────────────────────────

def test_classify_accepted_is_passed() -> None:
    assert classify_status(_raw(STATUS_ACCEPTED)) == TestCaseOutcome.PASSED


def test_classify_wrong_answer_is_failed() -> None:
    assert classify_status(_raw(STATUS_WRONG_ANSWER)) == TestCaseOutcome.FAILED


def test_classify_compile_error() -> None:
    assert classify_status(_raw(STATUS_COMPILE_ERROR)) == TestCaseOutcome.COMPILE_ERROR


def test_classify_time_limit_exceeded() -> None:
    assert classify_status(_raw(STATUS_TIME_LIMIT_EXCEEDED)) == TestCaseOutcome.TIME_LIMIT_EXCEEDED


def test_classify_output_limit_exceeded_is_flagged_not_swallowed() -> None:
    """Output-flooding scenario: a submission that floods stdout must be
    classified distinctly, not silently accepted or mis-scored as passed."""
    assert classify_status(_raw(STATUS_OUTPUT_LIMIT_EXCEEDED)) == TestCaseOutcome.OUTPUT_LIMIT_EXCEEDED


def test_classify_every_runtime_error_variant() -> None:
    for status_id in range(STATUS_RUNTIME_ERROR_MIN, STATUS_RUNTIME_ERROR_MAX + 1):
        assert classify_status(_raw(status_id)) == TestCaseOutcome.RUNTIME_ERROR


def test_classify_internal_error_is_infra_not_a_code_failure() -> None:
    assert classify_status(_raw(STATUS_INTERNAL_ERROR)) == TestCaseOutcome.INFRA_ERROR


def test_classify_exec_format_error_is_infra_not_a_code_failure() -> None:
    assert classify_status(_raw(STATUS_EXEC_FORMAT_ERROR)) == TestCaseOutcome.INFRA_ERROR


def test_classify_unknown_status_fails_safe_to_infra_error() -> None:
    """A status id this server has never seen must never be silently scored
    zero — fail safe to retryable infra error instead."""
    assert classify_status(_raw(999)) == TestCaseOutcome.INFRA_ERROR


def test_is_infra_error() -> None:
    assert is_infra_error(TestCaseOutcome.INFRA_ERROR) is True
    assert is_infra_error(TestCaseOutcome.FAILED) is False
    assert is_infra_error(TestCaseOutcome.PASSED) is False


# ── aggregate_weighted_score ──────────────────────────────────────────────────

def _result(test_id: str, outcome: TestCaseOutcome, weight: float = 1.0, is_hidden: bool = False) -> TestCaseResult:
    return TestCaseResult(test_id=test_id, group_name="g", is_hidden=is_hidden, weight=weight, outcome=outcome)


def test_aggregate_weighted_score_mixed_pass_fail() -> None:
    results = [
        _result("t1", TestCaseOutcome.PASSED, weight=2.0),
        _result("t2", TestCaseOutcome.FAILED, weight=1.0),
        _result("t3", TestCaseOutcome.PASSED, weight=1.0),
    ]
    earned, max_score = aggregate_weighted_score(results)
    assert earned == 3.0
    assert max_score == 4.0


def test_aggregate_weighted_score_all_failed_is_zero_earned() -> None:
    results = [_result("t1", TestCaseOutcome.COMPILE_ERROR), _result("t2", TestCaseOutcome.RUNTIME_ERROR)]
    earned, max_score = aggregate_weighted_score(results)
    assert earned == 0.0
    assert max_score == 2.0


# ── grade_mcq_exact_match ─────────────────────────────────────────────────────

def test_mcq_exact_match() -> None:
    assert grade_mcq_exact_match("A", "A") is True


def test_mcq_match_strips_surrounding_whitespace() -> None:
    assert grade_mcq_exact_match("  A  ", "A") is True


def test_mcq_mismatch() -> None:
    assert grade_mcq_exact_match("B", "A") is False


def test_mcq_is_case_sensitive() -> None:
    assert grade_mcq_exact_match("a", "A") is False


# ── new_grading_result / create_regrade ───────────────────────────────────────

def test_new_grading_result_computes_score_and_defaults_version_one() -> None:
    results = [_result("t1", TestCaseOutcome.PASSED), _result("t2", TestCaseOutcome.FAILED)]
    grading = new_grading_result(
        submission_id="sub-1", candidate_id="cand-1", run_id="run-1", uid="u1", question_id="q1",
        test_results=results, submission_hash="hash", runtime_image="img", test_suite_version=1, now=NOW,
    )
    assert grading.version == 1
    assert grading.weighted_score == 1.0
    assert grading.max_score == 2.0
    assert grading.graded_by is None
    assert grading.regrade_reason is None


def test_create_regrade_increments_version_and_does_not_mutate_previous() -> None:
    original_results = [_result("t1", TestCaseOutcome.FAILED)]
    previous = new_grading_result(
        submission_id="sub-1", candidate_id="cand-1", run_id="run-1", uid="u1", question_id="q1",
        test_results=original_results, submission_hash="hash", runtime_image="img", test_suite_version=1, now=NOW,
    )
    previous_snapshot = previous.model_copy(deep=True)

    new_results = [_result("t1", TestCaseOutcome.PASSED)]
    regraded = create_regrade(previous, test_results=new_results, now=NOW, graded_by="fac-1", reason="rubric fix")

    assert regraded.version == 2
    assert regraded.id != previous.id
    assert regraded.weighted_score == 1.0
    assert regraded.graded_by == "fac-1"
    assert regraded.regrade_reason == "rubric fix"
    # The original document's own fields must be completely untouched.
    assert previous == previous_snapshot


# ── Student-safe DTOs (hidden-data leakage prevention) ────────────────────────

def test_public_test_result_dto_includes_stdout_but_no_expected_output_field() -> None:
    result = _result("t1", TestCaseOutcome.FAILED).model_copy(update={"stdout": "1 2 3", "stderr": "warn"})
    dto = public_test_result_dto(result)
    assert dto["stdout"] == "1 2 3"
    assert dto["stderr"] == "warn"
    assert "expected_output" not in dto


def test_sealed_grade_summary_dto_never_leaks_hidden_stdout_stderr() -> None:
    """Security-critical: build a grading result whose hidden test outputs
    contain a distinctive marker, and assert that marker can never appear
    anywhere in the sealed student-facing summary."""
    secret_marker = "SECRET_HIDDEN_TEST_STDOUT_MARKER_4f9a"
    hidden_results = [
        TestCaseResult(
            test_id="hidden-1", group_name="hidden-group", is_hidden=True, weight=5.0,
            outcome=TestCaseOutcome.FAILED, stdout=secret_marker, stderr=secret_marker,
        ),
        TestCaseResult(
            test_id="hidden-2", group_name="hidden-group", is_hidden=True, weight=5.0,
            outcome=TestCaseOutcome.PASSED, stdout="also secret",
        ),
    ]
    grading = GradingResult(
        id="g1", submission_id="sub-1", candidate_id="cand-1", run_id="run-1", uid="u1", question_id="q1",
        test_results=hidden_results, weighted_score=5.0, max_score=10.0, submission_hash="h",
        runtime_image="img", test_suite_version=1, graded_at=NOW,
    )

    dto = sealed_grade_summary_dto(grading)

    assert secret_marker not in str(dto)
    assert set(dto.keys()) == {
        "question_id", "weighted_score", "max_score", "tests_passed", "tests_total", "version",
    }
    assert dto["tests_passed"] == 1
    assert dto["tests_total"] == 2
    assert dto["weighted_score"] == 5.0
    assert dto["max_score"] == 10.0
