"""Faculty command service — thin transactional wiring around
`decide_run_command`. See module docstring in `decisions.py` for why the
tricky logic lives there instead of here."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional
from uuid import uuid4

from firebase_admin import firestore

from app.domain.exams.clock import Clock, system_clock
from app.domain.exams.decisions import CommandOutcome, decide_run_command
from app.domain.exams.models import AttemptStatus, ExamDefinition, ExamPolicy, ExamRevision, ExamRun, RunStatus
from app.domain.exams.repository import (
    AttemptRepository,
    CommandReceiptRepository,
    ExamDefinitionRepository,
    ExamRunRepository,
    OutboxRepository,
    new_outbox_entry,
)

logger = logging.getLogger("hirelog.exams.commands")

_EXTENDABLE_ATTEMPT_STATUSES = frozenset(
    {AttemptStatus.ADMITTED, AttemptStatus.IN_PROGRESS, AttemptStatus.BLOCKED}
)


class ExamCommandService:
    def __init__(self, db, clock: Clock = system_clock) -> None:
        self._db = db
        self._clock = clock
        self.runs = ExamRunRepository(db)
        self.definitions = ExamDefinitionRepository(db)
        self._attempts = AttemptRepository(db)
        self._receipts = CommandReceiptRepository(db)
        self._outbox = OutboxRepository(db)

    def create_definition(
        self, *, title: str, owner_uid: str, institution_id: Optional[str] = None
    ) -> ExamDefinition:
        now = self._clock.now()
        definition = ExamDefinition(
            id=uuid4().hex, title=title, owner_uid=owner_uid, institution_id=institution_id,
            created_at=now, updated_at=now,
        )
        self.definitions.create(definition)
        return definition

    def publish_revision(self, *, definition_id: str, slot_count: int, published_by: str) -> ExamRevision:
        """Publishing creates a new, immutable revision. Never edits a prior
        one — faculty preview/navigation of an old revision can never turn
        into a mutation of it."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> ExamRevision:
            now = self._clock.now()
            definition = self.definitions.get_in_transaction(transaction, definition_id)
            if definition is None:
                raise LookupError(f"exam definition {definition_id} not found")

            revision_number = 1
            if definition.latest_published_revision_id:
                previous = self.definitions.get_revision_in_transaction(
                    transaction, definition_id, definition.latest_published_revision_id
                )
                if previous is not None:
                    revision_number = previous.revision_number + 1

            revision = ExamRevision(
                id=uuid4().hex,
                definition_id=definition_id,
                revision_number=revision_number,
                title=definition.title,
                published_at=now,
                published_by=published_by,
                slot_count=slot_count,
            )
            self.definitions.create_revision_in_transaction(transaction, revision)
            self.definitions.set_latest_revision_in_transaction(transaction, definition_id, revision.id, now)
            return revision

        return _txn(transaction)

    def create_run(
        self,
        *,
        definition_id: str,
        revision: ExamRevision,
        policy: ExamPolicy,
        enrolled_uids: list[str],
        institution_id: Optional[str] = None,
    ) -> ExamRun:
        """Publishing/scheduling a run. Enrollment is captured as an immutable
        snapshot at this moment (see ADR 0008 — stand-in for Prompt 2's
        institution/roster membership repository)."""
        now = self._clock.now()
        run = ExamRun(
            id=uuid4().hex,
            definition_id=definition_id,
            revision_id=revision.id,
            institution_id=institution_id,
            status=RunStatus.DRAFT,
            version=1,
            policy=policy,
            enrolled_uids=list(dict.fromkeys(enrolled_uids)),  # de-dupe, preserve order
            created_at=now,
            updated_at=now,
        )
        self.runs.create(run)
        return run

    def execute(
        self,
        *,
        run_id: str,
        command: str,
        expected_version: int,
        idempotency_key: str,
        force: bool = False,
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
                # drafts" contract — a plain (non-transactional) read is
                # acceptable here since it only affects whether we warn; the
                # actual state transition is still guarded by expected_version.
                unresolved = len(self._attempts.list_in_progress(run_id))

            outcome = decide_run_command(
                run=run,
                command=command,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                now=now,
                existing_receipt=existing_receipt,
                unresolved_attempt_count=unresolved,
                force=force,
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

            return outcome

        outcome = _txn(transaction)
        if outcome.applied:
            ExamRunRepository.invalidate(run_id)  # students must see the new status on their very next call
        return outcome

    def grant_time_extension(self, *, run_id: str, uid: str, extra_minutes: int) -> None:
        """Faculty-authored, audited time extension. Adjusts the attempt's
        deadline in one transaction (concurrent extensions add up instead of
        overwriting each other) and only for an attempt that is still live —
        a submitted/expired/finalized attempt is never reopened."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> None:
            attempt = self._attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is None:
                raise LookupError(f"attempt {uid} not found for run {run_id}")
            if attempt.status not in _EXTENDABLE_ATTEMPT_STATUSES:
                raise PermissionError("attempt_not_extendable")
            update: dict = {"manual_extension_minutes": attempt.manual_extension_minutes + extra_minutes}
            if attempt.deadline is not None:
                update["deadline"] = (attempt.deadline + timedelta(minutes=extra_minutes)).isoformat()
            self._attempts.update_in_transaction(transaction, run_id, uid, update)

        _txn(transaction)
