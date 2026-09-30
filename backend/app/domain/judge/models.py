"""Coding-judge domain models.

`CodeSubmission` never carries anything the student could use to influence
network settings, compiler flags, image IDs, or callback URLs — those are
always server-derived from `RuntimeAllowlistEntry`. Every result is bound to
`submission_hash` + `candidate_id` (variant) + `runtime_image` +
`test_suite_version` (contract: "Bind every result to submission hash,
variant, runtime image and test-suite version.").
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class RuntimeAllowlistEntry(BaseModel):
    """One allowlisted language. `validated_against_provider` is only ever
    set True by an operator action that actually queried a real provider's
    `/languages` endpoint — never assumed true by default."""

    language_key: str  # e.g. "python3.11" — the ONLY thing a student ever sends
    judge0_language_id: int
    display_name: str
    runtime_image: str
    validated_against_provider: bool = False


class TestCaseSpec(BaseModel):
    stdin: str = ""
    expected_output: str = ""


class TestGroupSpec(BaseModel):
    group_name: str
    weight: float = Field(gt=0)
    tests: list[TestCaseSpec] = Field(default_factory=list)
    is_hidden: bool = False


class SubmissionPurpose(str, Enum):
    PUBLIC_RUN = "public_run"  # quota'd, student-visible, public tests only
    FINAL_GRADE = "final_grade"  # sealed, all groups, triggered by attempt finalization


class SubmissionStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    POLLING = "polling"
    SUCCEEDED = "succeeded"
    INFRA_ERROR = "infra_error"  # retryable — never a silent zero score


class TestCaseOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    COMPILE_ERROR = "compile_error"
    RUNTIME_ERROR = "runtime_error"
    TIME_LIMIT_EXCEEDED = "time_limit_exceeded"
    MEMORY_LIMIT_EXCEEDED = "memory_limit_exceeded"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"
    INFRA_ERROR = "infra_error"  # provider-side failure, not the student's code


CODE_FAILURE_OUTCOMES = frozenset(
    {
        TestCaseOutcome.FAILED,
        TestCaseOutcome.COMPILE_ERROR,
        TestCaseOutcome.RUNTIME_ERROR,
        TestCaseOutcome.TIME_LIMIT_EXCEEDED,
        TestCaseOutcome.MEMORY_LIMIT_EXCEEDED,
        TestCaseOutcome.OUTPUT_LIMIT_EXCEEDED,
    }
)


class TestCaseResult(BaseModel):
    test_id: str
    group_name: str
    is_hidden: bool
    weight: float
    outcome: TestCaseOutcome
    # stdout/stderr/expected_output are intentionally NOT modeled here for
    # hidden tests at the point of student-facing serialization — see
    # `serialization.py::public_run_result_dto` / `sealed_grade_summary_dto`.
    stdout: str = ""
    stderr: str = ""
    time_ms: Optional[int] = None
    memory_kb: Optional[int] = None


class CodeSubmission(BaseModel):
    id: str
    candidate_id: str  # the generation candidate (variant) this answers
    run_id: str
    uid: str
    question_id: str
    purpose: SubmissionPurpose
    language_key: str
    source_code: str = Field(max_length=200_000)
    submission_hash: str  # sha256 of source_code — binds results to exact code
    runtime_image: str
    test_suite_version: int
    status: SubmissionStatus = SubmissionStatus.PENDING
    provider_tokens: dict[str, str] = Field(default_factory=dict)  # test_id -> provider token
    public_run_results: list[TestCaseResult] = Field(default_factory=list)
    attempts: int = 0
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class GradingResult(BaseModel):
    """Immutable once created — a regrade creates a NEW version doc, never
    edits this one (contract: "Regrades create explicit versions")."""

    id: str
    submission_id: str
    candidate_id: str
    run_id: str
    uid: str
    question_id: str
    version: int = 1
    test_results: list[TestCaseResult] = Field(default_factory=list)
    weighted_score: float = 0.0
    max_score: float = 0.0
    submission_hash: str
    runtime_image: str
    test_suite_version: int
    graded_at: datetime
    graded_by: Optional[str] = None  # None for automatic judge grading
    regrade_reason: Optional[str] = None


class McqGradeResult(BaseModel):
    """Same immutable-plus-explicit-regrade-version pattern as
    `GradingResult` (Prompt 9: 'auditable lineage', 'never overwrite')."""

    id: str
    run_id: str
    uid: str
    question_id: str
    candidate_id: str
    correct: bool
    version: int = 1
    graded_at: datetime
    graded_by: Optional[str] = None  # None for automatic exact-match grading
    regrade_reason: Optional[str] = None


class ShortAnswerReviewStatus(str, Enum):
    PENDING = "pending"
    REVIEWED = "reviewed"


class ShortAnswerReview(BaseModel):
    id: str
    run_id: str
    uid: str
    question_id: str
    candidate_id: str
    student_answer: str
    status: ShortAnswerReviewStatus = ShortAnswerReviewStatus.PENDING
    score: Optional[float] = None
    max_score: float = 0.0
    rubric_notes: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    version: int = 1
    created_at: datetime
