"""Pure grading logic: Judge0-status classification (code failure vs infra
error), weighted-group scoring, MCQ exact-match, and student-safe DTOs that
strip hidden-test stdout/stderr/expected-output by construction — not by a
runtime redaction step that could be forgotten.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

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


def classify_status(raw: JudgeRawResult) -> TestCaseOutcome:
    """Infrastructure errors (provider-side) are never scored as a code
    failure — the caller keeps the submission pending/retryable instead."""
    status = raw.status_id
    if status == STATUS_ACCEPTED:
        return TestCaseOutcome.PASSED
    if status == STATUS_WRONG_ANSWER:
        return TestCaseOutcome.FAILED
    if status == STATUS_COMPILE_ERROR:
        return TestCaseOutcome.COMPILE_ERROR
    if status == STATUS_TIME_LIMIT_EXCEEDED:
        return TestCaseOutcome.TIME_LIMIT_EXCEEDED
    if status == STATUS_OUTPUT_LIMIT_EXCEEDED:
        return TestCaseOutcome.OUTPUT_LIMIT_EXCEEDED
    if STATUS_RUNTIME_ERROR_MIN <= status <= STATUS_RUNTIME_ERROR_MAX:
        return TestCaseOutcome.RUNTIME_ERROR
    if status in (STATUS_INTERNAL_ERROR, STATUS_EXEC_FORMAT_ERROR):
        return TestCaseOutcome.INFRA_ERROR
    return TestCaseOutcome.INFRA_ERROR  # unknown status — fail safe to retryable, never silent zero


def is_infra_error(outcome: TestCaseOutcome) -> bool:
    return outcome == TestCaseOutcome.INFRA_ERROR


def aggregate_weighted_score(results: list[TestCaseResult]) -> tuple[float, float]:
    """Returns (weighted_score, max_score). A group with any infra-error test
    is excluded from BOTH sums by the caller before this runs (see
    `worker.py`) — this function assumes every result here is a final,
    scoreable outcome."""
    max_score = sum(r.weight for r in results)
    earned = sum(r.weight for r in results if r.outcome == TestCaseOutcome.PASSED)
    return earned, max_score


def _choice_keys(value: object) -> Optional[frozenset[str]]:
    """Normalises a stored answer / answer key to a set of option keys.
    A list/tuple/set (what the exam UI sends for multiple select) or a
    comma-separated string ("A,C", the answer-key format) are both accepted;
    anything else, or any non-string member, is not a valid answer (None)."""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        if not all(isinstance(item, str) for item in value):
            return None
        return frozenset(item.strip() for item in value if item.strip())
    return None


def grade_mcq_exact_match(student_answer: object, expected_answer: object) -> bool:
    """All-or-nothing: the selected option set must equal the answer-key set
    exactly. No partial credit and no negative marking (scoring semantics,
    ADR 0026; partial credit is open decision D-8). An empty answer or an
    empty answer key is never correct."""
    selected, expected = _choice_keys(student_answer), _choice_keys(expected_answer)
    if not selected or not expected:
        return False
    return selected == expected


def new_grading_result(
    *,
    submission_id: str,
    candidate_id: str,
    run_id: str,
    uid: str,
    question_id: str,
    test_results: list[TestCaseResult],
    submission_hash: str,
    runtime_image: str,
    test_suite_version: int,
    now: datetime,
    version: int = 1,
    graded_by: Optional[str] = None,
    regrade_reason: Optional[str] = None,
    result_id: Optional[str] = None,
) -> GradingResult:
    earned, max_score = aggregate_weighted_score(test_results)
    return GradingResult(
        id=result_id or uuid4().hex,
        submission_id=submission_id,
        candidate_id=candidate_id,
        run_id=run_id,
        uid=uid,
        question_id=question_id,
        version=version,
        test_results=test_results,
        weighted_score=earned,
        max_score=max_score,
        submission_hash=submission_hash,
        runtime_image=runtime_image,
        test_suite_version=test_suite_version,
        graded_at=now,
        graded_by=graded_by,
        regrade_reason=regrade_reason,
    )


def create_regrade(
    previous: GradingResult,
    *,
    test_results: list[TestCaseResult],
    now: datetime,
    graded_by: str,
    reason: str,
    result_id: Optional[str] = None,
) -> GradingResult:
    """Never mutates `previous` — returns a brand-new result with
    `version = previous.version + 1`. The caller persists both; the original
    submitted code/version lineage is preserved exactly."""
    return new_grading_result(
        submission_id=previous.submission_id,
        candidate_id=previous.candidate_id,
        run_id=previous.run_id,
        uid=previous.uid,
        question_id=previous.question_id,
        test_results=test_results,
        submission_hash=previous.submission_hash,
        runtime_image=previous.runtime_image,
        test_suite_version=previous.test_suite_version,
        now=now,
        version=previous.version + 1,
        graded_by=graded_by,
        regrade_reason=reason,
        result_id=result_id,
    )


# ── Student-safe DTOs (hidden-data leakage prevention) ───────────────────────

def public_test_result_dto(result: TestCaseResult) -> dict:
    """Only ever called for `is_hidden=False` results — students see their
    own stdout/stderr for PUBLIC tests only, never expected_output."""
    return {
        "test_id": result.test_id,
        "group_name": result.group_name,
        "outcome": result.outcome.value,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "time_ms": result.time_ms,
        "memory_kb": result.memory_kb,
    }


def sealed_grade_summary_dto(grading: GradingResult) -> dict:
    """The ONLY thing a student's final-grade view ever receives: aggregate
    counts, never per-hidden-test stdout/stderr/expected_output. Public-test
    results within a sealed grade are also summarized only — the detailed
    view for those was already available via the pre-submit public run."""
    passed = sum(1 for r in grading.test_results if r.outcome.value == "passed")
    total = len(grading.test_results)
    return {
        "question_id": grading.question_id,
        "weighted_score": grading.weighted_score,
        "max_score": grading.max_score,
        "tests_passed": passed,
        "tests_total": total,
        "version": grading.version,
    }
