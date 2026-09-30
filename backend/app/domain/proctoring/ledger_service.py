"""Proctoring ledger service — thin transactional wiring around
`correlate_observation`/`close_stale_open_incident`. All correlation/grace/
cooldown/threshold logic lives in `decisions.py`; this module only reads
fresh state inside a transaction, calls the pure function, and persists the
result — mirroring the exact pattern established in `student_service.py`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import uuid4

from firebase_admin import firestore

from app.domain.exams.clock import Clock, system_clock
from app.domain.exams.models import AttemptStatus
from app.domain.exams.repository import AttemptRepository, ExamRunRepository
from app.domain.exams.state_machine import require_attempt_transition
from app.domain.proctoring.decisions import CorrelationOutcome, close_stale_open_incident, correlate_observation
from app.domain.proctoring.errors import AttemptNotBlockedError
from app.domain.proctoring.models import IncidentLogEntry, ObservationKind, ProctoringState
from app.domain.proctoring.repository import IncidentLogRepository, ProctoringStateRepository

logger = logging.getLogger("hirelog.exams.proctoring")


def _empty_state(run_id: str, uid: str, now: datetime) -> ProctoringState:
    return ProctoringState(run_id=run_id, uid=uid, updated_at=now)


class ProctoringLedgerService:
    def __init__(self, db, clock: Clock = system_clock) -> None:
        self._db = db
        self._clock = clock
        self.states = ProctoringStateRepository(db)
        self.log = IncidentLogRepository(db)
        self.attempts = AttemptRepository(db)
        self.runs = ExamRunRepository(db)

    def record_observation(
        self,
        *,
        run_id: str,
        uid: str,
        kind: ObservationKind,
        client_event_id: str,
        client_timestamp: datetime,
        grace_seconds: float,
        cooldown_seconds: float,
        threshold: int,
    ) -> CorrelationOutcome:
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> CorrelationOutcome:
            now = self._clock.now()
            state = self.states.get_in_transaction(transaction, run_id, uid) or _empty_state(run_id, uid, now)
            outcome = correlate_observation(
                state=state, kind=kind, client_event_id=client_event_id, client_timestamp=client_timestamp,
                now=now, grace_seconds=grace_seconds, cooldown_seconds=cooldown_seconds, threshold=threshold,
            )
            if outcome.action == "deduped":
                return outcome

            self.states.upsert_in_transaction(transaction, outcome.new_state)
            if outcome.log_entry is not None:
                self.log.create_in_transaction(transaction, outcome.log_entry)

            count_changed = outcome.new_state.counted_incident_count != state.counted_incident_count
            if count_changed or outcome.should_block:
                attempt = self.attempts.get_in_transaction(transaction, run_id, uid)
                if attempt is not None:
                    update = {"warning_count": outcome.new_state.counted_incident_count, "updated_at": now.isoformat()}
                    if outcome.should_block and attempt.status != AttemptStatus.BLOCKED:
                        require_attempt_transition(attempt.status, AttemptStatus.BLOCKED)
                        update["status"] = AttemptStatus.BLOCKED.value
                        update["version"] = attempt.version + 1
                    self.attempts.update_in_transaction(transaction, run_id, uid, update)
            return outcome

        return _txn(transaction)

    def sweep_stale_incidents_once(self, *, grace_seconds: float, stale_after_seconds: float, limit: int = 50) -> int:
        """Bounded, idempotent, safe to call repeatedly — closes incidents
        for students who left the tab entirely without an end event ever
        arriving (contract #11: durable/restart-safe workers)."""
        closed = 0
        for state in self.states.list_open_incidents(limit=limit):
            run = self.runs.get(state.run_id)
            if run is None:
                continue
            transaction = self._db.transaction()

            @firestore.transactional
            def _txn(transaction, run_id=state.run_id, uid=state.uid, threshold=run.policy.warning_threshold) -> Optional[CorrelationOutcome]:
                now = self._clock.now()
                fresh = self.states.get_in_transaction(transaction, run_id, uid)
                if fresh is None:
                    return None
                outcome = close_stale_open_incident(
                    state=fresh, now=now, grace_seconds=grace_seconds, threshold=threshold, stale_after_seconds=stale_after_seconds,
                )
                if outcome is None:
                    return None
                self.states.upsert_in_transaction(transaction, outcome.new_state)
                if outcome.log_entry is not None:
                    self.log.create_in_transaction(transaction, outcome.log_entry)
                if outcome.should_block:
                    attempt = self.attempts.get_in_transaction(transaction, run_id, uid)
                    if attempt is not None and attempt.status != AttemptStatus.BLOCKED:
                        require_attempt_transition(attempt.status, AttemptStatus.BLOCKED)
                        self.attempts.update_in_transaction(
                            transaction, run_id, uid,
                            {"status": AttemptStatus.BLOCKED.value, "warning_count": outcome.new_state.counted_incident_count, "version": attempt.version + 1, "updated_at": now.isoformat()},
                        )
                return outcome

            result = _txn(transaction)
            if result is not None:
                closed += 1
        return closed

    def resolve_block(self, *, run_id: str, uid: str, resolved_by: str, allow_continue: bool, notes: str) -> None:
        """Explicit resolution only — never automatic, never resets the
        ledger, never reopens a FINALIZED attempt (contract: 'Unblock
        requires explicit resolution or an audited allowance; never reset
        the ledger or reopen final answers implicitly.')."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> None:
            now = self._clock.now()
            attempt = self.attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is None:
                raise LookupError(f"no attempt for run={run_id} uid={uid}")
            if attempt.status != AttemptStatus.BLOCKED:
                raise AttemptNotBlockedError(f"attempt for uid={uid} is not currently blocked")

            # "Uphold" makes no status change at all — the block simply
            # stands. Only "allow to continue" actively unblocks. Neither
            # path ever jumps straight to FINALIZED (not a legal transition
            # from BLOCKED) — an upheld block that outlives the deadline is
            # picked up by the ordinary finalization sweep instead, exactly
            # like any other still-open attempt.
            if allow_continue:
                require_attempt_transition(attempt.status, AttemptStatus.IN_PROGRESS)
                self.attempts.update_in_transaction(
                    transaction, run_id, uid,
                    {"status": AttemptStatus.IN_PROGRESS.value, "version": attempt.version + 1, "updated_at": now.isoformat()},
                )

            self.log.create_in_transaction(
                transaction,
                IncidentLogEntry(
                    id=uuid4().hex, run_id=run_id, uid=uid, kind=ObservationKind.PAGE_VISIBLE, counted=False,
                    client_timestamp=now, received_at=now, client_event_id=f"resolve-{uuid4().hex}",
                    note=f"faculty resolution by {resolved_by}: {'allowed to continue' if allow_continue else 'upheld — block stands'}. {notes}".strip(),
                ),
            )

        _txn(transaction)
