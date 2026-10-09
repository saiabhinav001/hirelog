"""Faculty command service - thin transactional wiring around the pure
decisions in `decisions.py`. See that module's docstring for why the tricky
logic lives there instead of here.

Every state-changing command here follows the same pattern (ADR 0008, 0019):
read fresh state INSIDE the transaction, decide with a pure function, then
write the state change + command receipt + audit event + outbox row together
or not at all. Duplicate retries are answered from the receipt.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from firebase_admin import firestore

from app.core.config import settings
from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.exams.clock import Clock, system_clock
from app.domain.exams.decisions import (
    CommandOutcome,
    decide_access_change,
    decide_recovery,
    decide_run_command,
    decide_terminate_attempt,
    decide_time_extension,
)
from app.domain.exams.errors import CommandRejectedError, PublicationBlockedError
from app.domain.exams.models import (
    CommandReceipt,
    ExamDefinition,
    ExamPolicy,
    ExamRevision,
    ExamRun,
    RunStatus,
)
from app.domain.exams.publication import build_preview, group_approved_by_slot, snapshot_slots
from app.domain.exams.repository import (
    AttemptRepository,
    CommandReceiptRepository,
    DraftRepository,
    ExamDefinitionRepository,
    ExamRunRepository,
    OutboxRepository,
    new_outbox_entry,
)
from app.domain.generation.repository import GenerationRepository

logger = logging.getLogger("hirelog.exams.commands")

SYSTEM_SCHEDULER_ACTOR = "system:schedule-automation"


@dataclass(frozen=True)
class ExtensionResult:
    uid: str
    deadline: str
    manual_extension_minutes: int
    reused_existing_receipt: bool = False


@dataclass(frozen=True)
class AttemptCommandResult:
    command: str
    uid: str
    reused_existing_receipt: bool = False
    changed: bool = True
    status: Optional[str] = None


def _revision_id_for(definition_id: str, idempotency_key: str) -> str:
    """Deterministic id for an idempotent publish: a retry with the same key
    addresses the SAME revision document instead of minting another."""
    return hashlib.sha256(f"{definition_id}:{idempotency_key}".encode("utf-8")).hexdigest()[:32]


class ExamCommandService:
    def __init__(self, db, clock: Clock = system_clock) -> None:
        self._db = db
        self._clock = clock
        self.runs = ExamRunRepository(db)
        self.definitions = ExamDefinitionRepository(db)
        self._attempts = AttemptRepository(db)
        self._drafts = DraftRepository(db)
        self._receipts = CommandReceiptRepository(db)
        self._outbox = OutboxRepository(db)
        self._audit = AuthzAuditRepository(db)
        self._generation = GenerationRepository(db)

    # -- helpers -------------------------------------------------------------

    def _audit_in_txn(
        self, transaction, kind: str, *, run: Optional[ExamRun], actor_uid: Optional[str], uid: str = "",
        detail: dict, now,
    ) -> None:
        self._audit.append_in_transaction(
            transaction,
            build_audit_event(
                kind, uid=uid, actor_uid=actor_uid, institution_id=run.institution_id if run else None,
                detail={k: v for k, v in detail.items() if v is not None}, now=now,
            ),
        )

    def _receipt_for(self, run: ExamRun, command: str, key: str, now, *, actor_uid, reason) -> CommandReceipt:
        return CommandReceipt(
            run_id=run.id, command=command, idempotency_key=key, applied_at=now,
            resulting_status=run.status, resulting_version=run.version, actor_uid=actor_uid, reason=reason,
        )

    # -- definitions / revisions / runs -------------------------------------

    def create_definition(self, *, title: str, owner_uid: str, institution_id: str | None = None) -> ExamDefinition:
        now = self._clock.now()
        definition = ExamDefinition(
            id=uuid4().hex, title=title, owner_uid=owner_uid, institution_id=institution_id,
            created_at=now, updated_at=now,
        )
        self.definitions.create(definition)
        return definition

    def preview_publication(self, *, definition_id: str, blueprint_id: str, duration_minutes: int | None = None):
        """Read-only: resolved counts, total marks, blockers and warnings for
        publishing `blueprint_id` as the next revision of `definition_id`."""
        blueprint = self._generation.get_blueprint(blueprint_id)
        if blueprint is None:
            raise LookupError(f"blueprint {blueprint_id} not found")
        if blueprint.exam_definition_id != definition_id:
            raise CommandRejectedError("publish", "blueprint_definition_mismatch")
        approved = group_approved_by_slot(self._generation.list_candidates_by_blueprint(blueprint_id))
        preview = build_preview(
            blueprint, approved, duration_minutes=duration_minutes,
            negative_marking_enabled=settings.EXAM_NEGATIVE_MARKING_ENABLED,
        )
        return blueprint, approved, preview

    def publish_revision(
        self,
        *,
        definition_id: str,
        slot_count: Optional[int] = None,
        published_by: str,
        blueprint_id: Optional[str] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> ExamRevision:
        """Publishing creates a new, immutable revision. Never edits a prior
        one - faculty preview/navigation of an old revision can never turn
        into a mutation of it.

        With `blueprint_id` the slot count, marks and approved-candidate ids
        are derived from the blueprint and frozen into the revision; blockers
        (e.g. a slot with no approved candidate) refuse the publish. Without
        it the legacy `slot_count`-only path is unchanged."""
        snapshot = None
        total_marks = None
        if blueprint_id:
            blueprint, approved, preview = self.preview_publication(
                definition_id=definition_id, blueprint_id=blueprint_id
            )
            if preview.blockers:
                raise PublicationBlockedError(preview.blockers)
            if slot_count is not None and slot_count != preview.slot_count:
                raise CommandRejectedError("publish", "slot_count_mismatch")
            slot_count = preview.slot_count
            total_marks = preview.total_marks
            snapshot = snapshot_slots(blueprint, approved)
        elif slot_count is None:
            raise ValueError("slot_count or blueprint_id is required")

        revision_id = _revision_id_for(definition_id, idempotency_key) if idempotency_key else uuid4().hex
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> ExamRevision:
            now = self._clock.now()
            definition = self.definitions.get_in_transaction(transaction, definition_id)
            if definition is None:
                raise LookupError(f"exam definition {definition_id} not found")

            if idempotency_key:
                replay = self.definitions.get_revision_in_transaction(transaction, definition_id, revision_id)
                if replay is not None:
                    return replay  # duplicate retry: same revision, nothing written

            revision_number = 1
            if definition.latest_published_revision_id:
                previous = self.definitions.get_revision_in_transaction(
                    transaction, definition_id, definition.latest_published_revision_id
                )
                if previous is not None:
                    revision_number = previous.revision_number + 1

            revision = ExamRevision(
                id=revision_id,
                definition_id=definition_id,
                revision_number=revision_number,
                title=definition.title,
                published_at=now,
                published_by=published_by,
                slot_count=slot_count,
                blueprint_id=blueprint_id or None,
                total_marks=total_marks,
                slots=snapshot,
                publish_reason=reason,
            )
            self.definitions.create_revision_in_transaction(transaction, revision)
            self.definitions.set_latest_revision_in_transaction(transaction, definition_id, revision.id, now)
            self._audit.append_in_transaction(
                transaction,
                build_audit_event(
                    "exam_revision_published", actor_uid=published_by, institution_id=definition.institution_id,
                    detail={
                        "definition_id": definition_id, "revision_id": revision.id,
                        "revision_number": revision_number, "slot_count": slot_count,
                        "blueprint_id": blueprint_id, "reason": reason,
                    },
                    now=now,
                ),
            )
            return revision

        return _txn(transaction)

    def create_run(
        self,
        *,
        definition_id: str,
        revision: ExamRevision,
        policy: ExamPolicy,
        enrolled_uids: list[str],
        institution_id: str | None = None,
        actor_uid: str | None = None,
    ) -> ExamRun:
        """Publishing/scheduling a run. Enrollment is captured as an immutable
        snapshot at this moment (see ADR 0008 - stand-in for Prompt 2's
        institution/roster membership repository). The run is pinned to
        `revision.id`; later revisions never change it."""
        now = self._clock.now()
        run = ExamRun(
            id=uuid4().hex,
            definition_id=definition_id,
            revision_id=revision.id,
            status=RunStatus.DRAFT,
            version=1,
            policy=policy,
            enrolled_uids=list(dict.fromkeys(enrolled_uids)),  # de-dupe, preserve order
            institution_id=institution_id,
            created_at=now,
            updated_at=now,
        )
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> None:
            self.runs.create_in_transaction(transaction, run)
            self._audit_in_txn(
                transaction, "exam_run_created", run=run, actor_uid=actor_uid,
                detail={
                    "run_id": run.id, "definition_id": definition_id, "revision_id": revision.id,
                    "enrolled_count": len(run.enrolled_uids),
                }, now=now,
            )

        _txn(transaction)
        return run

    # -- run lifecycle ---------------------------------------------------------

    def execute(
        self,
        *,
        run_id: str,
        command: str,
        expected_version: int,
        idempotency_key: str,
        force: bool = False,
        actor_uid: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> CommandOutcome:
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> CommandOutcome:
            now = self._clock.now()
            run = self.runs.get_in_transaction(transaction, run_id)
            if run is None:
                raise LookupError(f"exam run {run_id} not found")

            existing_receipt = self._receipts.get_in_transaction(
                transaction, run_id, command, idempotency_key
            )

            unresolved = 0
            if command == "close" and existing_receipt is None:
                # Best-effort count for the "warn before closing unresolved
                # drafts" contract - a plain (non-transactional) read is
                # acceptable here since it only affects whether we warn; the
                # actual state transition is still guarded by expected_version.
                # Attempts already past their own deadline are not "unresolved":
                # the finalization sweeper seals them. Accommodated attempts
                # that are still inside their (longer) deadline ARE counted, so
                # closing a cohort needs an explicit `force` and never cuts
                # them off - close only stops new admissions.
                unresolved = sum(
                    1 for a in self._attempts.list_in_progress(run_id)
                    if a.deadline is None or now <= a.deadline
                )

            outcome = decide_run_command(
                run=run,
                command=command,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                now=now,
                existing_receipt=existing_receipt,
                unresolved_attempt_count=unresolved,
                force=force,
                actor_uid=actor_uid,
                reason=reason,
            )

            if outcome.applied:
                extra: dict = {}
                if command == "release":
                    extra["released_at"] = now.isoformat()
                elif command == "close":
                    extra["closed_at"] = now.isoformat()
                elif command == "end":
                    extra["ended_at"] = now.isoformat()

                self.runs.apply_command_in_transaction(
                    transaction,
                    run_id,
                    new_status=outcome.receipt.resulting_status,
                    new_version=outcome.receipt.resulting_version,
                    now=now,
                    extra=extra,
                )
                self._receipts.create_in_transaction(transaction, outcome.receipt)
                entry = new_outbox_entry(
                    kind="run_command",
                    run_id=run_id,
                    payload={
                        "command": command,
                        "resulting_status": outcome.receipt.resulting_status.value,
                    },
                    now=now,
                )
                self._outbox.add_in_transaction(transaction, entry)
                self._audit_in_txn(
                    transaction, "exam_run_command", run=run, actor_uid=actor_uid,
                    detail={
                        "run_id": run_id, "command": command, "from_status": run.status.value,
                        "to_status": outcome.receipt.resulting_status.value,
                        "resulting_version": outcome.receipt.resulting_version,
                        "forced": True if force else None, "reason": reason,
                        "idempotency_key": idempotency_key,
                    },
                    now=now,
                )

            return outcome

        return _txn(transaction)

    # -- per-attempt / per-student commands ------------------------------------

    def grant_time_extension(
        self,
        *,
        run_id: str,
        uid: str,
        extra_minutes: int,
        actor_uid: Optional[str] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> ExtensionResult:
        """Faculty-authored, audited time extension of one live attempt.

        Runs in one transaction so it is serialised against the finalization
        sweeper and against another extension of the same attempt: an
        extension granted before the deadline is never lost, one attempted
        after it (or after sealing) is rejected - see `decide_time_extension`.
        With an `idempotency_key` a duplicate retry returns the first result
        instead of adding the minutes twice."""
        command = f"extend:{uid}"
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> ExtensionResult:
            now = self._clock.now()
            run = self.runs.get_in_transaction(transaction, run_id)
            if run is None:
                raise LookupError(f"exam run {run_id} not found")
            attempt = self._attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is None:
                raise LookupError(f"attempt {uid} not found for run {run_id}")

            if idempotency_key:
                replay = self._receipts.get_in_transaction(transaction, run_id, command, idempotency_key)
                if replay is not None:
                    return ExtensionResult(
                        uid=uid, deadline=attempt.deadline.isoformat() if attempt.deadline else "",
                        manual_extension_minutes=attempt.manual_extension_minutes, reused_existing_receipt=True,
                    )

            decision = decide_time_extension(attempt=attempt, run=run, now=now, extra_minutes=extra_minutes)
            self._attempts.update_in_transaction(
                transaction, run_id, uid,
                {
                    "manual_extension_minutes": decision.new_manual_extension_minutes,
                    "deadline": decision.new_deadline.isoformat(),
                    "version": attempt.version + 1,
                    "updated_at": now.isoformat(),
                },
            )
            if idempotency_key:
                self._receipts.create_in_transaction(
                    transaction,
                    self._receipt_for(run, command, idempotency_key, now, actor_uid=actor_uid, reason=reason),
                )
            self._audit_in_txn(
                transaction, "exam_attempt_extended", run=run, actor_uid=actor_uid, uid=uid,
                detail={
                    "run_id": run_id, "extra_minutes": extra_minutes,
                    "previous_deadline": attempt.deadline.isoformat() if attempt.deadline else None,
                    "new_deadline": decision.new_deadline.isoformat(),
                    "total_manual_extension_minutes": decision.new_manual_extension_minutes,
                    "reason": reason, "idempotency_key": idempotency_key,
                },
                now=now,
            )
            return ExtensionResult(
                uid=uid, deadline=decision.new_deadline.isoformat(),
                manual_extension_minutes=decision.new_manual_extension_minutes,
            )

        return _txn(transaction)

    def terminate_attempt(
        self, *, run_id: str, uid: str, idempotency_key: str, actor_uid: str, reason: str
    ) -> AttemptCommandResult:
        """Faculty seals one student's attempt now: EXPIRED + receipt over the
        last acknowledged drafts, then the normal grading trigger. Never a
        reopening; a second call (same key) is answered from its receipt."""
        command = f"terminate:{uid}"
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> AttemptCommandResult:
            now = self._clock.now()
            run = self.runs.get_in_transaction(transaction, run_id)
            if run is None:
                raise LookupError(f"exam run {run_id} not found")
            attempt = self._attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is None:
                raise LookupError(f"attempt {uid} not found for run {run_id}")
            if self._receipts.get_in_transaction(transaction, run_id, command, idempotency_key) is not None:
                return AttemptCommandResult(
                    command="terminate", uid=uid, reused_existing_receipt=True, changed=False,
                    status=attempt.status.value,
                )

            decision = decide_terminate_attempt(
                attempt=attempt, now=now, idempotency_key=idempotency_key,
                current_draft_versions=self._drafts.all_versions(run_id, uid),
            )
            self._attempts.update_in_transaction(
                transaction, run_id, uid,
                {
                    "status": decision.new_status.value,
                    "version": attempt.version + 1,
                    "submit_receipt": decision.receipt.model_dump(mode="json"),
                    "finalization_reason": "faculty_terminated",
                    "finalized_by": actor_uid,
                    "updated_at": now.isoformat(),
                },
            )
            self._outbox.add_in_transaction(
                transaction,
                new_outbox_entry(
                    kind="attempt_finalized", run_id=run_id,
                    payload={"uid": uid, "receipt_id": decision.receipt.receipt_id, "reason": "faculty_terminated"},
                    now=now,
                ),
            )
            self._receipts.create_in_transaction(
                transaction, self._receipt_for(run, command, idempotency_key, now, actor_uid=actor_uid, reason=reason)
            )
            self._audit_in_txn(
                transaction, "exam_attempt_terminated", run=run, actor_uid=actor_uid, uid=uid,
                detail={
                    "run_id": run_id, "from_status": attempt.status.value, "reason": reason,
                    "receipt_id": decision.receipt.receipt_id, "idempotency_key": idempotency_key,
                },
                now=now,
            )
            return AttemptCommandResult(command="terminate", uid=uid, status=decision.new_status.value)

        return _txn(transaction)

    def change_access(
        self, *, run_id: str, action: str, uid: str, idempotency_key: str, actor_uid: str, reason: Optional[str]
    ) -> AttemptCommandResult:
        """Grant or revoke ONE student's access to a run. Institution
        membership of a granted uid is checked by the caller (route layer)."""
        command = f"{action}:{uid}"
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> AttemptCommandResult:
            now = self._clock.now()
            run = self.runs.get_in_transaction(transaction, run_id)
            if run is None:
                raise LookupError(f"exam run {run_id} not found")
            if self._receipts.get_in_transaction(transaction, run_id, command, idempotency_key) is not None:
                return AttemptCommandResult(command=action, uid=uid, reused_existing_receipt=True, changed=False)
            attempt = self._attempts.get_in_transaction(transaction, run_id, uid)

            decision = decide_access_change(run=run, action=action, uid=uid, attempt=attempt)
            if decision.changed:
                self.runs.set_enrolled_in_transaction(transaction, run_id, decision.enrolled_uids, now)
                self._audit_in_txn(
                    transaction, "exam_access_granted" if action == "grant" else "exam_access_revoked",
                    run=run, actor_uid=actor_uid, uid=uid,
                    detail={"run_id": run_id, "reason": reason, "idempotency_key": idempotency_key}, now=now,
                )
            self._receipts.create_in_transaction(
                transaction, self._receipt_for(run, command, idempotency_key, now, actor_uid=actor_uid, reason=reason)
            )
            return AttemptCommandResult(command=action, uid=uid, changed=decision.changed)

        return _txn(transaction)

    def recover_attempt(
        self, *, run_id: str, uid: str, idempotency_key: str, actor_uid: str, reason: str
    ) -> AttemptCommandResult:
        """Re-drive the grading trigger for an attempt that is already sealed.
        Never reopens an attempt and never edits drafts or receipts."""
        command = f"recover:{uid}"
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> AttemptCommandResult:
            now = self._clock.now()
            run = self.runs.get_in_transaction(transaction, run_id)
            if run is None:
                raise LookupError(f"exam run {run_id} not found")
            attempt = self._attempts.get_in_transaction(transaction, run_id, uid)
            if self._receipts.get_in_transaction(transaction, run_id, command, idempotency_key) is not None:
                return AttemptCommandResult(
                    command="recover", uid=uid, reused_existing_receipt=True, changed=False,
                    status=attempt.status.value if attempt else None,
                )
            decide_recovery(attempt=attempt)
            self._outbox.add_in_transaction(
                transaction,
                new_outbox_entry(
                    kind="attempt_finalized", run_id=run_id,
                    payload={
                        "uid": uid, "reason": "recovery",
                        "receipt_id": attempt.submit_receipt.receipt_id if attempt.submit_receipt else None,
                    },
                    now=now,
                ),
            )
            self._receipts.create_in_transaction(
                transaction, self._receipt_for(run, command, idempotency_key, now, actor_uid=actor_uid, reason=reason)
            )
            self._audit_in_txn(
                transaction, "exam_attempt_recovery_requested", run=run, actor_uid=actor_uid, uid=uid,
                detail={"run_id": run_id, "status": attempt.status.value, "reason": reason, "idempotency_key": idempotency_key},
                now=now,
            )
            return AttemptCommandResult(command="recover", uid=uid, status=attempt.status.value)

        return _txn(transaction)
