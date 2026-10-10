"""Gradebook, similarity, export, and learning-feedback domain models.

Firestore layout (all private — no client rule grants them; default-deny
catch-all covers everything here):

  gradebook_releases/{release_id}
  gradebook_release_rows/{snapshot_id}__{uid}
  gradebook_defective_questions/{resolution_id}
  gradebook_grade_appeals/{appeal_id}
  similarity_matches/{match_id}
  gradebook_export_jobs/{job_id}
  gradebook_export_tickets/{ticket_id}
  gradebook_export_access_log/{entry_id}
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class QuestionOutcome(str, Enum):
    """The exact four content-outcome categories the prompt names, plus
    BLANK/PENDING which are prerequisites to even asking "how well". A
    question is one and only one of these at any given moment."""

    BLANK = "blank"
    PENDING = "pending"
    PARTIALLY_SOLVED = "partially_solved"
    FULLY_SOLVED = "fully_solved"


class FailureCategory(str, Enum):
    """Coding-only — the worst test-case outcome seen, so a faculty member
    scanning the gradebook knows WHY a coding question scored low without
    opening every submission."""

    NONE = "none"
    WRONG_ANSWER = "wrong_answer"
    COMPILE_ERROR = "compile_error"
    RUNTIME_ERROR = "runtime_error"
    TIME_LIMIT_EXCEEDED = "time_limit_exceeded"
    MEMORY_LIMIT_EXCEEDED = "memory_limit_exceeded"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"


class StudentQuestionResult(BaseModel):
    """One gradebook cell: one student, one question."""

    uid: str
    question_id: str
    slot_index: int
    item_type: str
    presented: bool
    outcome: QuestionOutcome
    weighted_score: float = 0.0
    max_score: float = 0.0
    grading_version: int = 1
    # Coding-only detail (None for single_choice/multiple_select/short_answer).
    tests_passed: Optional[int] = None
    tests_total: Optional[int] = None
    failure_category: Optional[FailureCategory] = None
    submission_hash: Optional[str] = None
    runtime_image: Optional[str] = None
    test_suite_version: Optional[int] = None


class IntegrityDisposition(str, Enum):
    """Deliberately separate from `raw_score` — never blended into the
    academic grade (contract: 'Raw academic grade and integrity disposition
    are separate.')."""

    CLEAN = "clean"
    BLOCKED_PENDING_REVIEW = "blocked_pending_review"
    BLOCKED_UPHELD = "blocked_upheld"
    BLOCKED_RESOLVED_CONTINUED = "blocked_resolved_continued"


class StudentGradebookRow(BaseModel):
    uid: str
    attempt_status: str
    finalized: bool
    raw_score: float
    raw_max_score: float
    integrity_disposition: IntegrityDisposition
    warning_count: int
    question_results: list[StudentQuestionResult] = Field(default_factory=list)


class GradebookCounts(BaseModel):
    """Aggregate across every assigned student, for one question."""

    question_id: str
    assigned: int = 0
    presented: int = 0
    finalized: int = 0
    nonblank_attempted: int = 0
    fully_solved: int = 0
    partially_solved: int = 0
    blank: int = 0
    pending: int = 0


class ReleaseRecord(BaseModel):
    """A gradebook release is versioned independently of any one question's
    grading version — releasing v2 after a regrade is what actually shows
    students the corrected result (contract: 'release version... corrected
    results with auditable lineage')."""

    id: str
    run_id: str
    version: int
    released_by: str
    released_at: datetime
    note: str = ""
    # What the students are shown for this version: a frozen copy of every enrolled student's row, taken at
    # release time (`gradebook_release_rows/{snapshot_id}__{uid}`). None on releases made before snapshots
    # existed; those are served live, exactly as before.
    snapshot_id: Optional[str] = None
    row_count: int = 0
    # Honest state at release time, so a later reader can tell a complete release from a partial one.
    pending_question_count: int = 0
    live_attempt_count: int = 0
    acknowledged_incomplete: bool = False


class DefectiveQuestionResolution(BaseModel):
    id: str
    run_id: str
    question_id: str
    resolution: Literal["full_credit_all", "exclude_from_total"]
    reason: str = Field(max_length=1000)
    resolved_by: str
    resolved_at: datetime


class GradeAppealStatus(str, Enum):
    PENDING = "pending"
    UPHELD = "upheld"  # original grade stands
    CORRECTED = "corrected"  # a regrade was applied as a result


class GradeAppeal(BaseModel):
    """Separate from the proctoring `Appeal` (ADR 0013) — this disputes a
    SCORE, not a warning/block."""

    id: str
    run_id: str
    uid: str
    question_id: str
    student_statement: str = Field(max_length=4000)
    status: GradeAppealStatus = GradeAppealStatus.PENDING
    created_at: datetime
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None
    version: int = 1
    # The release version the student was looking at when they disputed it.
    release_version: Optional[int] = None


class ExportFormat(str, Enum):
    CSV = "csv"
    JSON = "json"


class ExportJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExportJob(BaseModel):
    id: str
    run_id: str
    requested_by: str
    format: ExportFormat
    status: ExportJobStatus = ExportJobStatus.PENDING
    created_at: datetime
    completed_at: Optional[datetime] = None
    storage_ref: Optional[str] = None
    row_count: Optional[int] = None
    error: Optional[str] = None
    # The downloadable content is deleted at `expires_at`; `expired_at` records that it was. The job record
    # itself (who asked, when, how many rows) is kept.
    expires_at: Optional[datetime] = None
    expired_at: Optional[datetime] = None


class ExportDownloadTicket(BaseModel):
    id: str
    export_job_id: str
    issued_to: str
    expires_at: datetime
    consumed: bool = False
    consumed_at: Optional[datetime] = None


class ExportAccessLogEntry(BaseModel):
    id: str
    export_job_id: str
    accessed_by: str
    accessed_at: datetime


class SimilarityMatch(BaseModel):
    """Grouped by (run_id, question_id, language) — instructor starter code
    is excluded before comparison, never merely down-weighted. Shown only to
    authorized reviewers; never an automatic misconduct decision or score
    penalty (contract #10/Prompt 9)."""

    id: str
    run_id: str
    question_id: str
    language: str
    uid_a: str
    uid_b: str
    submission_id_a: str
    submission_id_b: str
    similarity_percent: float = Field(ge=0, le=100)
    matched_spans: list[dict] = Field(default_factory=list)
    provider: str  # "deterministic_fake" | "jplag_cli"
    computed_at: datetime


class PracticeStep(BaseModel):
    """One practice step tied to an objective ID that was supplied from the question's own record."""

    objective_id: str = Field(max_length=100)
    step: str = Field(max_length=300)


class LearningFeedback(BaseModel):
    """Deterministic, rule-based — not a live LLM call (contract: 'Optional
    AI signals remain disabled until locally evaluated.'). Assembled only
    from ALREADY-released rubric outcomes and source anchors; never hidden
    tests, unreleased answers, or another student's data."""

    run_id: str
    uid: str
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    practice_steps: list[str] = Field(default_factory=list)
    practice_sequence: list[PracticeStep] = Field(default_factory=list)
    source_anchors: list[str] = Field(default_factory=list)
    missing_material: list[str] = Field(default_factory=list)
    generated_at: datetime
