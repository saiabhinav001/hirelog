"""Bounded finalization sweep — seals expired attempts in small batches, never
in one enormous transaction. Safe to call repeatedly (e.g. from a scheduled
worker tick); each call processes at most `batch_size` attempts and each
attempt is sealed in its OWN transaction, so a crash mid-sweep loses at most
one partially-applied batch item, not the whole run.
"""

from __future__ import annotations

import logging

from firebase_admin import firestore

from app.domain.exams.clock import Clock, system_clock
from app.domain.exams.decisions import decide_finalization_batch
from app.domain.exams.repository import AttemptRepository, DraftRepository, OutboxRepository, new_outbox_entry
from app.domain.exams.state_machine import require_attempt_transition

logger = logging.getLogger("hirelog.exams.finalization")


class ExamFinalizationSweeper:
    def __init__(self, db, clock: Clock = system_clock) -> None:
        self._db = db
        self._clock = clock
        self._attempts = AttemptRepository(db)
        self._drafts = DraftRepository(db)
        self._outbox = OutboxRepository(db)

    def sweep_once(self, run_id: str, *, batch_size: int) -> int:
        """One bounded pass. Returns the number of attempts sealed this call
        — callers loop until this returns 0."""
        now = self._clock.now()
        candidates = self._attempts.list_in_progress(run_id)
        draft_versions_by_uid = {
            attempt.uid: self._drafts.all_versions(run_id, attempt.uid) for attempt in candidates
        }
        batch = decide_finalization_batch(
            attempts=candidates,
            now=now,
            draft_versions_by_uid=draft_versions_by_uid,
            batch_size=batch_size,
        )

        sealed = 0
        for decision in batch.sealed:
            if self._seal_one(run_id, decision.uid):
                sealed += 1
        return sealed

    def _seal_one(self, run_id: str, uid: str) -> bool:
        """Re-decides fresh inside its own transaction — the batch-level
        decision above only selects CANDIDATES; the actual seal independently
        re-checks deadline/state so a delayed sweep never seals early or
        double-seals (every write independently checks deadlines/state)."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> bool:
            now = self._clock.now()
            attempt = self._attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is None:
                return False
            from app.domain.exams.decisions import decide_seal_on_expiry

            versions = self._drafts.all_versions(run_id, uid)
            decision = decide_seal_on_expiry(attempt=attempt, now=now, current_draft_versions=versions)
            if decision is None:
                return False

            require_attempt_transition(attempt.status, decision.new_status)
            self._attempts.update_in_transaction(
                transaction,
                run_id,
                uid,
                {
                    "status": decision.new_status.value,
                    "version": attempt.version + 1,
                    "updated_at": now.isoformat(),
                },
            )
            entry = new_outbox_entry(
                kind="attempt_expired",
                run_id=run_id,
                payload={"uid": uid},
                now=now,
            )
            self._outbox.add_in_transaction(transaction, entry)
            return True

        return _txn(transaction)
