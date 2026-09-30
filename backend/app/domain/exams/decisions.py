"""Pure decision functions for exam commands, admission, and final submission.

These contain ALL of the tricky concurrency/idempotency/deadline logic and
have zero Firestore dependency, so every scenario in the Prompt 3/4 test list
(duplicate/stale commands, stale epoch, draft-version conflicts, deadline
races, accommodation preservation, ...) is unit-testable directly against
plain dataclasses/models. The API/repository layer re-runs the SAME functions
inside a real Firestore transaction so the decision is made against a fresh
read on every retry — this module never calls a clock or a database itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from app.domain.exams.errors import UnresolvedAttemptsError
from app.domain.exams.models import (
    Attempt,
    AttemptStatus,
    CommandReceipt,
    ExamRun,
    RESOLVED_ATTEMPT_STATUSES,
    RunStatus,
    SubmitReceipt,
)
from app.domain.exams.state_machine import (
    RUN_COMMAND_TARGET,
    can_transition_attempt,
    require_run_transition,
)


# ── Faculty commands ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CommandOutcome:
    applied: bool
    receipt: CommandReceipt
    reused_existing_receipt: bool = False


def decide_run_command(
    *,
    run: ExamRun,
    command: str,
    expected_version: int,
    idempotency_key: str,
    now: datetime,
    existing_receipt: Optional[CommandReceipt],
    unresolved_attempt_count: int = 0,
    force: bool = False,
) -> CommandOutcome:
    """Decide the outcome of one faculty command. Raises on illegal state;
    returns a `CommandOutcome` when it can be (or already was) applied."""

    if existing_receipt is not None:
        # Duplicate/stale command replay — return exactly what was already
        # decided, never re-apply (idempotent command receipts).
        return CommandOutcome(applied=False, receipt=existing_receipt, reused_existing_receipt=True)

    if run.version != expected_version:
        from app.domain.exams.errors import VersionConflictError

        raise VersionConflictError(expected_version, run.version)

    target = RUN_COMMAND_TARGET.get(command)
    if target is None:
        raise ValueError(f"Unknown exam run command: {command!r}")

    require_run_transition(run.status, target)

    if command == "close" and unresolved_attempt_count > 0 and not force:
        raise UnresolvedAttemptsError(unresolved_attempt_count)

    receipt = CommandReceipt(
        run_id=run.id,
        command=command,
        idempotency_key=idempotency_key,
        applied_at=now,
        resulting_status=target,
        resulting_version=run.version + 1,
    )
    return CommandOutcome(applied=True, receipt=receipt)


# ── Admission / deadlines ─────────────────────────────────────────────────────

def compute_deadline(
    *,
    admitted_at: datetime,
    duration_minutes: int,
    accommodation_extra_minutes: int = 0,
    manual_extension_minutes: int = 0,
) -> datetime:
    total_minutes = duration_minutes + accommodation_extra_minutes + manual_extension_minutes
    return admitted_at + timedelta(minutes=total_minutes)


# ── Final submission ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SubmitOutcome:
    accepted: bool
    receipt: Optional[SubmitReceipt]
    conflict_reason: Optional[str] = None
    reused_existing_receipt: bool = False


def check_epoch(attempt: Attempt, client_epoch: int) -> Optional[str]:
    """Returns a conflict reason string, or None if the epoch is current."""
    if client_epoch != attempt.epoch:
        return "stale_epoch"
    return None


def decide_submit(
    *,
    attempt: Attempt,
    run: ExamRun,
    now: datetime,
    idempotency_key: str,
    client_epoch: int,
    expected_draft_versions: dict[str, int],
    current_draft_versions: dict[str, int],
) -> SubmitOutcome:
    # Immutability: a finalized attempt is never reopened, by anyone,
    # including faculty preview/navigation (contract #4).
    if attempt.status == AttemptStatus.FINALIZED:
        existing = attempt.submit_receipt
        if existing is not None and existing.idempotency_key == idempotency_key:
            return SubmitOutcome(True, existing, reused_existing_receipt=True)
        return SubmitOutcome(False, None, conflict_reason="already_finalized")

    if attempt.status == AttemptStatus.BLOCKED:
        return SubmitOutcome(False, None, conflict_reason="attempt_blocked")

    if attempt.status != AttemptStatus.IN_PROGRESS:
        return SubmitOutcome(False, None, conflict_reason="attempt_not_in_progress")

    if run.status == RunStatus.ENDED:
        return SubmitOutcome(False, None, conflict_reason="run_ended")

    if run.status == RunStatus.PAUSED:
        return SubmitOutcome(False, None, conflict_reason="run_paused")

    epoch_conflict = check_epoch(attempt, client_epoch)
    if epoch_conflict:
        return SubmitOutcome(False, None, conflict_reason=epoch_conflict)

    if attempt.deadline is not None and now > attempt.deadline:
        return SubmitOutcome(False, None, conflict_reason="deadline_passed")

    if expected_draft_versions != current_draft_versions:
        return SubmitOutcome(False, None, conflict_reason="draft_version_conflict")

    receipt = SubmitReceipt(
        idempotency_key=idempotency_key,
        finalized_at=now,
        sealed_draft_versions=dict(current_draft_versions),
        receipt_id=uuid4().hex,
    )
    return SubmitOutcome(True, receipt)


@dataclass(frozen=True)
class DraftSaveOutcome:
    accepted: bool
    new_version: int = 0
    conflict_reason: Optional[str] = None


def decide_save_draft(
    *,
    attempt: Attempt,
    run: ExamRun,
    now: datetime,
    expected_version: int,
    current_version: int,
) -> DraftSaveOutcome:
    """Same conflict vocabulary as `decide_submit` so the API layer maps both
    to consistent HTTP responses. A stale autosave (expected_version behind
    current_version) is an explicit conflict, never a silent overwrite."""

    if attempt.status == AttemptStatus.BLOCKED:
        return DraftSaveOutcome(False, conflict_reason="attempt_blocked")
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return DraftSaveOutcome(False, conflict_reason="attempt_not_in_progress")
    if run.status == RunStatus.ENDED:
        return DraftSaveOutcome(False, conflict_reason="run_ended")
    if run.status == RunStatus.PAUSED:
        return DraftSaveOutcome(False, conflict_reason="run_paused")
    if attempt.deadline is not None and now > attempt.deadline:
        return DraftSaveOutcome(False, conflict_reason="deadline_passed")
    if expected_version != current_version:
        return DraftSaveOutcome(False, conflict_reason="draft_version_conflict")
    return DraftSaveOutcome(True, new_version=current_version + 1)


def is_content_reachable(*, run: ExamRun, attempt: Attempt, now: datetime) -> Optional[str]:
    """Gate for question-content delivery (never for drafts/submit, which have
    their own decisions above). Returns a conflict reason, or None if the
    student's CURRENT slot may be fetched right now.

    Deliberately ignores RunStatus.PAUSED here: closing/pausing a cohort must
    not cut off a still-admitted, still-within-deadline (possibly
    accommodated) student from seeing their own current question — only a
    run that was never released, or one that has hard-ended, blocks content.
    """

    if run.status in (RunStatus.DRAFT, RunStatus.PUBLISHED):
        return "run_not_released"
    if run.status == RunStatus.ENDED:
        return "run_ended"
    if attempt.status == AttemptStatus.BLOCKED:
        return "attempt_blocked"
    if attempt.status not in (AttemptStatus.ADMITTED, AttemptStatus.IN_PROGRESS):
        return "attempt_not_active"
    if attempt.deadline is not None and now > attempt.deadline:
        return "deadline_passed"
    return None


@dataclass(frozen=True)
class AdvanceSlotOutcome:
    accepted: bool
    new_slot_index: int = 0
    conflict_reason: Optional[str] = None


def decide_advance_slot(*, attempt: Attempt, run: ExamRun, total_slots: int, now: datetime) -> AdvanceSlotOutcome:
    """Forward-only — `current_slot_index` never decrements. This is the
    server-side backing for 'Keyboard Back/Next cannot access locked or
    rewrite submitted content': there is no server action that ever moves it
    backward, so no client control can either. The LAST slot has no 'next' —
    the student submits instead (contract #3/#4)."""
    reachable_reason = is_content_reachable(run=run, attempt=attempt, now=now)
    if reachable_reason is not None:
        return AdvanceSlotOutcome(False, conflict_reason=reachable_reason)
    if attempt.current_slot_index + 1 >= total_slots:
        return AdvanceSlotOutcome(False, conflict_reason="no_further_questions")
    return AdvanceSlotOutcome(True, new_slot_index=attempt.current_slot_index + 1)


# ── Bounded finalization sweep (close/expiry) ────────────────────────────────

@dataclass(frozen=True)
class SealDecision:
    uid: str
    new_status: AttemptStatus
    receipt: SubmitReceipt


def decide_seal_on_expiry(
    *,
    attempt: Attempt,
    now: datetime,
    current_draft_versions: dict[str, int],
) -> Optional[SealDecision]:
    """One attempt's slice of a bounded finalization sweep. Returns None if
    this attempt does not need sealing right now (e.g. still within an
    accommodated deadline, or already resolved) — every write independently
    re-checks deadline/state, so a delayed worker never seals early.

    BLOCKED is deliberately seal-eligible too (Prompt 8): a student whose
    exam window expires while blocked for review must still be sealed —
    faculty review/appeal happens against the preserved receipt afterward,
    never by leaving the attempt stuck open past its own deadline."""

    if attempt.status in RESOLVED_ATTEMPT_STATUSES:
        return None
    if attempt.status not in (AttemptStatus.IN_PROGRESS, AttemptStatus.BLOCKED):
        return None
    if attempt.deadline is None or now <= attempt.deadline:
        return None  # accommodated / not-yet-expired students are left alone

    if not can_transition_attempt(attempt.status, AttemptStatus.EXPIRED):
        return None

    receipt = SubmitReceipt(
        idempotency_key=f"auto-expiry:{attempt.uid}:{attempt.run_id}",
        finalized_at=now,
        sealed_draft_versions=dict(current_draft_versions),
        receipt_id=uuid4().hex,
    )
    return SealDecision(uid=attempt.uid, new_status=AttemptStatus.EXPIRED, receipt=receipt)


@dataclass(frozen=True)
class FinalizationBatchResult:
    sealed: list[SealDecision] = field(default_factory=list)
    skipped: int = 0

    @property
    def sealed_count(self) -> int:
        return len(self.sealed)


def decide_finalization_batch(
    *,
    attempts: list[Attempt],
    now: datetime,
    draft_versions_by_uid: dict[str, dict[str, int]],
    batch_size: int,
) -> FinalizationBatchResult:
    """Pure decision for ONE bounded batch — never processes the whole run in
    a single call, satisfying "bounded finalization jobs rather than one
    enormous transaction". Callers loop until `sealed_count == 0`."""

    sealed: list[SealDecision] = []
    skipped = 0
    for attempt in attempts:
        if len(sealed) >= batch_size:
            skipped += 1
            continue
        decision = decide_seal_on_expiry(
            attempt=attempt,
            now=now,
            current_draft_versions=draft_versions_by_uid.get(attempt.uid, {}),
        )
        if decision is not None:
            sealed.append(decision)
    return FinalizationBatchResult(sealed=sealed, skipped=skipped)
