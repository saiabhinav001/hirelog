"""Exam blueprint / question-generation domain models.

`GenerationRequest` deliberately has NO student-identity field anywhere in
its schema — "never send student PII to question generation" is enforced by
there being no field to put it in, not by a runtime check that could be
forgotten. `GenerationCandidate` deliberately has NO `approved` field the
generation provider could ever set — only a separate faculty command
(`review.py`) can move a candidate to APPROVED, and only `publish.py` can
move it to PUBLISHED (immutable after).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ItemType(str, Enum):
    SINGLE_CHOICE = "single_choice"
    MULTIPLE_SELECT = "multiple_select"
    SHORT_ANSWER = "short_answer"
    CODING = "coding"


class CandidateStatus(str, Enum):
    DRAFT = "draft"
    INSUFFICIENT_SOURCE = "insufficient_source"
    NEEDS_REVISION = "needs_revision"
    READY_FOR_HUMAN_REVIEW = "ready_for_human_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"  # terminal, immutable


TERMINAL_CANDIDATE_STATUSES = frozenset({CandidateStatus.PUBLISHED, CandidateStatus.REJECTED})


class BlueprintSlot(BaseModel):
    slot_index: int = Field(ge=0)
    item_type: ItemType
    objective_ids: list[str] = Field(min_length=1)
    difficulty: str
    marks: float = Field(gt=0)
    target_minutes: int = Field(gt=0, le=180)
    permitted_languages: list[str] = Field(default_factory=list)
    requested_variant_count: int = Field(ge=1, le=50)

    model_config = {"extra": "forbid"}


class ExamBlueprint(BaseModel):
    id: str
    exam_definition_id: str
    title: str = Field(min_length=1, max_length=200)
    slots: list[BlueprintSlot] = Field(min_length=1)
    created_by: str
    created_at: datetime
    updated_at: datetime


class SourceAnchorRef(BaseModel):
    source_id: str
    source_version: int
    anchor_id: str


class SourceAnchorWithExcerpt(SourceAnchorRef):
    excerpt: str = Field(max_length=4000)


class GenerationRequest(BaseModel):
    model_config = {"protected_namespaces": ()}  # `model_name` is a legitimate field, not pydantic's `model_*`

    id: str
    blueprint_id: str
    slot_index: int
    objective_ids: list[str]
    item_type: ItemType
    difficulty: str
    marks: float
    target_minutes: int
    requested_variant_count: int = Field(ge=1, le=50)
    permitted_languages: list[str] = Field(default_factory=list)
    approved_source_bundle: list[SourceAnchorWithExcerpt] = Field(default_factory=list)
    deterministic_seed: int
    created_by: str
    created_at: datetime
    status: Literal["pending", "running", "succeeded", "failed"] = "pending"
    attempts: int = 0
    last_error: Optional[str] = None
    model_name: Optional[str] = None
    prompt_version: str = "generation-prompt-a-v1"
    cost_estimate: Optional[float] = None


class ValidationIssue(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    field: str
    reason: str


class GenerationCandidate(BaseModel):
    id: str
    request_id: str
    blueprint_id: str
    slot_index: int
    family_id: str
    local_candidate_id: str
    item_type: ItemType
    objective_ids: list[str]
    difficulty: str
    marks: float
    estimated_minutes: int
    uniqueness_signature: str
    public_prompt: str = Field(max_length=8000)
    constraints: dict = Field(default_factory=dict)
    public_examples: list = Field(default_factory=list)
    options: list = Field(default_factory=list)
    private_expected_answer: Optional[str] = None
    private_explanation: Optional[str] = None
    marking_rubric: dict = Field(default_factory=dict)
    reference_solutions: dict[str, str] = Field(default_factory=dict)  # language -> code
    public_tests: list = Field(default_factory=list)
    supporting_anchors: list[SourceAnchorRef] = Field(default_factory=list)
    equivalence_rationale: str = ""
    status: CandidateStatus = CandidateStatus.DRAFT
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    created_at: datetime
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    published_at: Optional[datetime] = None
