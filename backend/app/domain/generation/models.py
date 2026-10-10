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

from pydantic import BaseModel, Field, model_validator


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
    # Prompt 5 (ADR 0021): replaced by a newer edited version; kept for history, never served.
    SUPERSEDED = "superseded"


TERMINAL_CANDIDATE_STATUSES = frozenset({CandidateStatus.PUBLISHED, CandidateStatus.REJECTED, CandidateStatus.SUPERSEDED})


class FamilyConstraints(BaseModel):
    """Faculty-authored limits on a slot's question family (Backend prompt A inputs).

    Optional and bounded. Sent to the provider as constraints (not as source
    text); `forbidden_topics` is also enforced locally on every candidate.
    """

    required_techniques: list[str] = Field(default_factory=list, max_length=10)
    forbidden_topics: list[str] = Field(default_factory=list, max_length=20)
    input_bounds: Optional[str] = Field(default=None, max_length=500)
    expected_complexity: Optional[str] = Field(default=None, max_length=200)
    parameter_ranges: dict[str, str] = Field(default_factory=dict, max_length=20)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _bounded_text(self):
        for label, values in (("required_techniques", self.required_techniques), ("forbidden_topics", self.forbidden_topics)):
            if any((not isinstance(v, str)) or not v.strip() or len(v) > 200 for v in values):
                raise ValueError(f"{label} entries must be non-empty text of at most 200 characters")
        if any(len(str(k)) > 60 or len(str(v)) > 200 for k, v in self.parameter_ranges.items()):
            raise ValueError("parameter_ranges keys must be at most 60 and values at most 200 characters")
        return self


class BlueprintSlot(BaseModel):
    slot_index: int = Field(ge=0)
    item_type: ItemType
    objective_ids: list[str] = Field(min_length=1)
    difficulty: str
    marks: float = Field(gt=0)
    target_minutes: int = Field(gt=0, le=180)
    permitted_languages: list[str] = Field(default_factory=list)
    requested_variant_count: int = Field(ge=1, le=50)
    # Prompt 3 (ADR 0019, decision D-8): marks deducted for a wrong answer.
    # Default 0 reproduces today's scoring. Non-zero values are rejected unless
    # EXAM_NEGATIVE_MARKING_ENABLED is set, because grading does not apply them
    # until the scoring semantics are decided.
    negative_marks: float = Field(default=0, ge=0)
    # Backend prompt A: optional family limits; None reproduces earlier behaviour.
    family_constraints: Optional[FamilyConstraints] = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _negative_marks_within_marks(self):
        if self.negative_marks > self.marks:
            raise ValueError("negative_marks cannot exceed marks")
        return self


class ExamBlueprint(BaseModel):
    id: str
    exam_definition_id: str
    title: str = Field(min_length=1, max_length=200)
    slots: list[BlueprintSlot] = Field(min_length=1)
    created_by: str
    created_at: datetime
    updated_at: datetime
    # Prompt 5 (ADR 0021) - all optional; defaults reproduce the earlier behavior.
    topics: list[str] = Field(default_factory=list, max_length=50)  # teacher-confirmed, never auto-applied
    topics_confirmed_by: Optional[str] = None
    duration_minutes: Optional[int] = Field(default=None, ge=1, le=24 * 60)
    language_allowlist: list[str] = Field(default_factory=list)  # narrows the server registry; empty = registry
    shuffle_options: bool = False  # per-student seeded option order for choice items
    variant_policy: Literal["unique", "balanced_reuse"] = "unique"
    # An explicit, audited acceptance of fewer approved variants than requested.
    shortfall_accepted: bool = False
    shortfall_accepted_by: Optional[str] = None
    shortfall_accepted_at: Optional[datetime] = None
    shortfall_reason: Optional[str] = None


class SourceAnchorRef(BaseModel):
    source_id: str
    source_version: int
    anchor_id: str


class SourceAnchorWithExcerpt(SourceAnchorRef):
    excerpt: str = Field(max_length=4000)
    # Prompt 5: where the passage sits and whether it looks like it is giving instructions.
    page: Optional[int] = None
    section: Optional[str] = None
    injection_flags: list[str] = Field(default_factory=list)


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
    family_constraints: Optional[FamilyConstraints] = None
    approved_source_bundle: list[SourceAnchorWithExcerpt] = Field(default_factory=list)
    deterministic_seed: int
    created_by: str
    created_at: datetime
    status: Literal["pending", "running", "succeeded", "failed"] = "pending"
    attempts: int = 0
    last_error: Optional[str] = None
    model_name: Optional[str] = None
    prompt_version: str = "generation-prompt-a-v2"  # v1 requests already stored keep their provenance
    cost_estimate: Optional[float] = None
    # Prompt 5 (ADR 0021): provider metadata, set after the call (the job id is `id`).
    provider_name: Optional[str] = None
    latency_ms: Optional[int] = None
    retries: int = 0
    bundle_sha256: Optional[str] = None  # hash of the exact excerpts that were sent
    regenerates_candidate_id: Optional[str] = None
    accepted_shortfall: Optional[int] = None  # variants the provider said it could not supply
    shuffle_options: bool = False  # copied from the blueprint: validation must reject position-dependent options
    # Signatures already used in this blueprint, shown to the provider so it avoids them. Never persisted.
    previous_signatures: list[str] = Field(default_factory=list, exclude=True)
    # Public prompts of the other live variants in the slot, shown to the independent reviewer only. Never persisted.
    sibling_public_prompts: list[str] = Field(default_factory=list, exclude=True)
    source_versions: dict[str, int] = Field(default_factory=dict)  # source_id -> extracted version number sent
    excluded_anchors: list[dict] = Field(default_factory=list)  # {source_id, anchor_id, reason} withheld from the provider
    usage: dict = Field(default_factory=dict)  # provider-reported tokens/cost, opaque
    completed_at: Optional[datetime] = None
    running_since: Optional[datetime] = None  # set while a worker holds the request; a stale hold can be taken over


class ValidationIssue(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    field: str
    reason: str


class IndependentReviewIssue(BaseModel):
    """One issue from "Backend model prompt B"'s independent review — unlike
    `ValidationIssue`, this can cite a `source_anchor` or `counterexample`,
    since it comes from a model that actually re-solved the item rather than
    a structural check."""

    severity: Literal["blocker", "major", "minor"]
    field: str
    reason: str
    source_anchor: Optional[SourceAnchorRef] = None
    counterexample: Optional[str] = None
    # Set by the backend, never by the model: a blocker/major claim with no anchor and no counterexample.
    evidence_missing: bool = False


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
    # Per-language boilerplate shown to the student in the code editor before
    # they write anything (e.g. a function signature). No existing authoring
    # path populates this yet, so it defaults to empty for every candidate —
    # a safe no-op. Reserved for Prompt 9's similarity review, which must
    # strip this shared text before comparing submissions (contract:
    # "excluding instructor starter code") so identical boilerplate across
    # every student never registers as a false-positive match.
    starter_code: dict[str, str] = Field(default_factory=dict)
    # Sealed grading input (Prompt 6) — never serialized to students. Shape:
    # [{"group_name": str, "weight": float, "tests": [{"stdin": str, "expected_output": str}]}]
    hidden_test_groups: list = Field(default_factory=list)
    supporting_anchors: list[SourceAnchorRef] = Field(default_factory=list)
    equivalence_rationale: str = ""
    # Backend prompt A: provider-reported notes for the reviewer. Informational only - never
    # served to students and never able to change status. Concerns also appear as minor issues.
    variant_parameters: dict = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    provider_concerns: list[str] = Field(default_factory=list)
    status: CandidateStatus = CandidateStatus.DRAFT
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    created_at: datetime
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    published_at: Optional[datetime] = None
    # Independent model review ("Backend model prompt B") — set at most once
    # by `IndependentReviewJobRunner`, never by faculty review. `agreement`
    # is informational only; `recommended_status` is what can move a
    # READY_FOR_HUMAN_REVIEW candidate back to NEEDS_REVISION — it can never
    # advance a candidate further (contract: "never published or verified").
    independent_review_agreement: Optional[Literal["agree", "disagree", "uncertain"]] = None
    independent_review_issues: list[IndependentReviewIssue] = Field(default_factory=list)
    independent_review_completed_at: Optional[datetime] = None
    independent_review_unexecuted_checks: list[str] = Field(default_factory=list)
    independent_review_prompt_version: Optional[str] = None
    # Prompt 5 (ADR 0021): an edit never mutates a candidate; it creates the next version.
    version: int = 1
    previous_version_id: Optional[str] = None
    superseded_by_id: Optional[str] = None
    edited_by: Optional[str] = None
    edited_at: Optional[datetime] = None
    published_revision_id: Optional[str] = None
    # True only when the reference solution and tests were executed in the isolated judge (decision D-5).
    execution_verified: bool = False
