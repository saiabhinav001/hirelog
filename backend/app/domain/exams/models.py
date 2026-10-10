"""Core exam domain models.

These are plain Pydantic models representing Firestore document shapes. They
carry no Firestore SDK dependency so the domain layer (state machine,
decisions) can be unit-tested with zero mocking.

Firestore layout (all collections are private — no client rule grants them;
the default-deny catch-all in firestore.rules covers everything here):

  exam_definitions/{def_id}
  exam_definitions/{def_id}/revisions/{revision_id}      (immutable once published)
  exam_runs/{run_id}
  exam_runs/{run_id}/attempts/{uid}
  exam_runs/{run_id}/attempts/{uid}/drafts/{question_id}
  exam_command_receipts/{run_id}__{command}__{idempotency_key}
  exam_outbox/{entry_id}
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from app.domain.exams.policy import ExamPolicy


class RunStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RELEASED = "released"
    PAUSED = "paused"
    CLOSED = "closed"
    ENDED = "ended"


class AttemptStatus(str, Enum):
    NOT_STARTED = "not_started"
    ADMITTED = "admitted"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    EXPIRED = "expired"
    # Reserved for Prompt 8 (proctoring incident ledger) — no current code path
    # sets this yet, but the submit/finalization decision logic already treats
    # it as a hard stop so Prompt 8 can slot in without touching this phase.
    BLOCKED = "blocked"
    FINALIZED = "finalized"


TERMINAL_ATTEMPT_STATUSES = frozenset({AttemptStatus.FINALIZED})
RESOLVED_ATTEMPT_STATUSES = frozenset(
    {AttemptStatus.SUBMITTED, AttemptStatus.EXPIRED, AttemptStatus.FINALIZED}
)


class ExamDefinition(BaseModel):
    """Draft, mutable authoring surface. Publishing snapshots a revision."""

    id: str
    title: str = Field(min_length=1, max_length=200)
    owner_uid: str
    created_at: datetime
    updated_at: datetime
    latest_published_revision_id: Optional[str] = None
    # Institution this exam belongs to (Prompt 1, ADR 0017). None = unscoped
    # pilot/legacy resource: only its owner and super-admins can manage it.
    institution_id: Optional[str] = None
    # Co-instructors ("definition = course", Prompt 2, ADR 0018): linked, ACTIVE
    # FACULTY/ADMIN members of `institution_id` assigned by a super-admin or an
    # ADMIN of that institution. They may manage this definition's runs,
    # blueprints and gradebook but not the instructor list itself.
    instructor_uids: list[str] = Field(default_factory=list, max_length=20)


class RevisionSlot(BaseModel):
    """One slot of a published revision - a frozen copy of the blueprint slot
    plus the exact approved candidate ids that were eligible at publish time.
    Later approvals/rejections on the blueprint never change a published
    revision ("active attempts remain pinned")."""

    slot_index: int = Field(ge=0)
    item_type: str
    difficulty: str
    marks: float = Field(gt=0)
    negative_marks: float = Field(default=0, ge=0)
    target_minutes: int = Field(gt=0)
    objective_ids: list[str] = Field(default_factory=list)
    permitted_languages: list[str] = Field(default_factory=list)
    requested_variant_count: int = Field(ge=1)
    approved_candidate_ids: list[str] = Field(default_factory=list)
    # "source_id:source_version:anchor_id" keys of the approved candidates' supporting anchors.
    source_refs: list[str] = Field(default_factory=list)


class ExamRevision(BaseModel):
    """Immutable once created - publishing a definition creates exactly one
    of these; it is never edited afterward."""

    id: str
    definition_id: str
    revision_number: int = Field(ge=1)
    title: str
    published_at: datetime
    published_by: str
    # Question ordering/content lives in a later phase (Prompt 4/5 question
    # bank); this phase only needs slot *count* and per-slot marks/duration to
    # drive the state machine and deadlines.
    slot_count: int = Field(ge=1)
    # Prompt 3 (ADR 0019) - present only when published from a blueprint;
    # legacy `slot_count`-only revisions leave all of these unset.
    blueprint_id: Optional[str] = None
    total_marks: Optional[float] = None
    slots: Optional[list[RevisionSlot]] = None
    publish_reason: Optional[str] = None
    # Prompt 5 (ADR 0021): frozen with the revision so a later blueprint change cannot alter a run.
    topics: Optional[list[str]] = None
    shuffle_options: Optional[bool] = None
    variant_policy: Optional[str] = None
    accepted_shortfall_slots: Optional[list[int]] = None  # slots published with fewer approved variants than requested
    shortfall_reason: Optional[str] = None


class ExamRun(BaseModel):
    """A scheduled, running instance of a published revision."""

    id: str
    definition_id: str
    revision_id: str
    status: RunStatus = RunStatus.DRAFT
    version: int = 1
    policy: ExamPolicy
    # Enrollment snapshot — SIMPLIFICATION (see ADR 0008): a direct UID list
    # captured at publish time, standing in for Prompt 2's institution/roster
    # membership repository. Swap the *source* of this snapshot later without
    # changing anything below this point.
    enrolled_uids: list[str] = Field(default_factory=list)
    # Copied from the definition at run creation (never client-supplied).
    # When set, students also need an ACTIVE membership in this institution.
    institution_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    released_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class SubmitReceipt(BaseModel):
    idempotency_key: str
    finalized_at: datetime
    sealed_draft_versions: dict[str, int] = Field(default_factory=dict)
    receipt_id: str


class Attempt(BaseModel):
    """One student's attempt at one run. Doc id == uid (single active attempt
    per student per run, per contract #4)."""

    run_id: str
    uid: str
    status: AttemptStatus = AttemptStatus.NOT_STARTED
    version: int = 1
    epoch: int = 0
    admitted_at: Optional[datetime] = None
    deadline: Optional[datetime] = None
    accommodation_extra_minutes: int = 0
    manual_extension_minutes: int = 0
    warning_count: int = 0
    current_slot_index: int = 0
    submit_receipt: Optional[SubmitReceipt] = None
    updated_at: Optional[datetime] = None
    # Prompt 3 (ADR 0019): why/who sealed an attempt that the student did not
    # submit themselves. None for a student submit and for legacy documents.
    # finalization_reason: "deadline_expired" | "faculty_terminated"
    finalization_reason: Optional[str] = None
    finalized_by: Optional[str] = None  # actor uid, or "system:finalization-sweeper"


class Draft(BaseModel):
    question_id: str
    version: int = 0
    answer: Optional[dict] = None
    updated_at: Optional[datetime] = None


class CommandReceipt(BaseModel):
    """Idempotent faculty-command result, keyed by (run_id, command, key)."""

    run_id: str
    command: str
    idempotency_key: str
    applied_at: datetime
    resulting_status: RunStatus
    resulting_version: int
    # Prompt 3 (ADR 0019): who issued the command and why. Optional so legacy
    # receipts and callers that send neither keep working.
    actor_uid: Optional[str] = None
    reason: Optional[str] = None


class OutboxEntry(BaseModel):
    """Transactional outbox row — written in the SAME transaction as the
    state mutation it describes. Delivery is at-least-once; handlers must be
    idempotent (contract: "idempotent worker interface")."""

    id: str
    kind: str  # e.g. "run_command", "attempt_finalized"
    run_id: str
    payload: dict = Field(default_factory=dict)
    created_at: datetime
    processed: bool = False
    processed_at: Optional[datetime] = None
    attempts: int = 0
    claimed_at: Optional[datetime] = None
    claimed_by: Optional[str] = None
    last_error: Optional[str] = None


class PublicQuestionRef(BaseModel):
    """Allowlisted student-facing question reference.

    SCOPE NOTE (Prompt 4): the real question bank (prompt text, options,
    code-editor spec) is built in Prompt 5. This phase only guarantees WHICH
    slot is reachable and WHEN — never leaking locked/future content — so
    Prompt 5 only has to populate `public_content` later without touching any
    access-control logic here.
    """

    question_id: str
    slot_index: int
    total_slots: int
    public_content: dict = Field(default_factory=dict)
