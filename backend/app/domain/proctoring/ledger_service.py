"""Proctoring ledger service — thin transactional wiring around
`correlate_observation`/`close_stale_open_incident`. All correlation/grace/
cooldown/threshold logic lives in `decisions.py`; this module only reads
fresh state inside a transaction, calls the pure function, and persists the
result — mirroring the exact pattern established in `student_service.py`.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from uuid import uuid4

from firebase_admin import firestore

from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.exams.clock import Clock, system_clock
from app.domain.exams.models import AttemptStatus
from app.domain.exams.repository import AttemptRepository, ExamRunRepository
from app.domain.exams.state_machine import require_attempt_transition
from app.domain.proctoring.decisions import (
    CorrelationOutcome,
    close_stale_open_incident,
    correlate_observation,
    effective_proctoring,
)
from app.domain.proctoring.errors import AttemptNotBlockedError
from app.domain.proctoring.models import IncidentLogEntry, ObservationKind, ProctoringState
from app.domain.proctoring.repository import IncidentLogRepository, ProctoringStateRepository

logger = logging.getLogger("hirelog.exams.proctoring")

# Observations only mean something while the attempt is live; afterwards they are recorded but never counted.
_LIVE_STATUSES = frozenset({AttemptStatus.IN_PROGRESS, AttemptStatus.BLOCKED})


def _empty_state(run_id: str, uid: str, now: datetime) -> ProctoringState:
    return ProctoringState(run_id=run_id, uid=uid, updated_at=now)


def _server_entry(*, entry_id: str, run_id: str, uid: str, kind: ObservationKind, now: datetime, client_timestamp: datetime,
                  client_event_id: str, note: str) -> IncidentLogEntry:
    return IncidentLogEntry(
        id=entry_id, run_id=run_id, uid=uid, kind=kind, counted=False, client_timestamp=client_timestamp,
        received_at=now, client_event_id=client_event_id, note=note, source="server",
    )


class ProctoringLedgerService:
    def __init__(self, db, clock: Clock = system_clock) -> None:
        self._db = db
        self._clock = clock
        self.states = ProctoringStateRepository(db)
        self.log = IncidentLogRepository(db)
        self.attempts = AttemptRepository(db)
        self.runs = ExamRunRepository(db)
        self._audit = AuthzAuditRepository(db)

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
            # The attempt is read first, in the same transaction as the decision: a submit or expiry that commits
            # first makes this event a late observation (recorded, never counted, never blocking); one that commits
            # later finds the attempt blocked.
            attempt = self.attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is None or attempt.status not in _LIVE_STATUSES:
                if client_event_id in state.recent_event_ids:
                    return CorrelationOutcome(action="deduped", new_state=state, log_entry=None)
                digest = hashlib.sha256(f"{run_id}|{uid}|{client_event_id}".encode()).hexdigest()[:24]
                entry = _server_entry(
                    entry_id=f"inactive-{digest}", run_id=run_id, uid=uid, kind=kind, now=now,
                    client_timestamp=client_timestamp, client_event_id=client_event_id,
                    note="received while the attempt was not active - recorded, not counted",
                )
                self.log.create_in_transaction(transaction, entry)  # deterministic id: a replay rewrites the same entry
                return CorrelationOutcome(action="ignored_inactive", new_state=state, log_entry=entry)

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
                update = {"warning_count": outcome.new_state.counted_incident_count, "updated_at": now.isoformat()}
                if outcome.should_block and attempt.status != AttemptStatus.BLOCKED:
                    require_attempt_transition(attempt.status, AttemptStatus.BLOCKED)
                    update["status"] = AttemptStatus.BLOCKED.value
                    update["version"] = attempt.version + 1
                self.attempts.update_in_transaction(transaction, run_id, uid, update)
            return outcome

        return _txn(transaction)

    def sweep_stale_incidents_once(self, *, grace_seconds: float, stale_after_seconds: float, limit: int = 50) -> int:
        """Bounded, idempotent, safe to call repeatedly — closes incidents for students who left the tab entirely
        without an end event ever arriving (contract #11: durable/restart-safe workers). Each state is handled on its
        own, so one that cannot be processed never stops the rest; the run's own grace period applies; an attempt
        that is no longer live has its incident closed without counting."""
        closed = 0
        for state in self.states.list_open_incidents(limit=limit):
            try:
                run = self.runs.get(state.run_id)
                if run is None:
                    continue
                run_grace = effective_proctoring(
                    run.policy, default_grace=grace_seconds, default_cooldown=0.0, default_retention_days=0,
                ).grace_seconds
                if self._close_stale_one(state.run_id, state.uid, run.policy.warning_threshold, run_grace, stale_after_seconds):
                    closed += 1
            except Exception as exc:  # noqa: BLE001 - one bad state must not starve the others
                logger.warning("proctoring sweep: %s/%s skipped: %s", state.run_id, state.uid, type(exc).__name__)
        return closed

    def _close_stale_one(self, run_id: str, uid: str, threshold: int, grace_seconds: float, stale_after_seconds: float) -> bool:
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> bool:
            now = self._clock.now()
            fresh = self.states.get_in_transaction(transaction, run_id, uid)
            if fresh is None:
                return False
            outcome = close_stale_open_incident(
                state=fresh, now=now, grace_seconds=grace_seconds, threshold=threshold, stale_after_seconds=stale_after_seconds,
            )
            if outcome is None:
                return False
            attempt = self.attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is None or attempt.status not in _LIVE_STATUSES:
                # Nothing to count against: close the open incident and keep the counters as they were.
                closed = fresh.model_copy(update={
                    "open_incident_started_at": None, "open_incident_last_activity_at": None, "open_incident_already_counted": False,
                    "version": fresh.version + 1, "updated_at": now,
                })
                self.states.upsert_in_transaction(transaction, closed)
                self.log.create_in_transaction(transaction, _server_entry(
                    entry_id=uuid4().hex, run_id=run_id, uid=uid, kind=ObservationKind.PAGE_HIDDEN, now=now,
                    client_timestamp=fresh.open_incident_last_activity_at or now, client_event_id=f"sweep-close-{uuid4().hex}",
                    note="open departure closed after the attempt ended - not counted",
                ))
                return True
            self.states.upsert_in_transaction(transaction, outcome.new_state)
            if outcome.log_entry is not None:
                self.log.create_in_transaction(transaction, outcome.log_entry.model_copy(update={"source": "server"}))
            if outcome.should_block and attempt.status != AttemptStatus.BLOCKED:
                require_attempt_transition(attempt.status, AttemptStatus.BLOCKED)
                self.attempts.update_in_transaction(
                    transaction, run_id, uid,
                    {"status": AttemptStatus.BLOCKED.value, "warning_count": outcome.new_state.counted_incident_count, "version": attempt.version + 1, "updated_at": now.isoformat()},
                )
            elif outcome.new_state.counted_incident_count != fresh.counted_incident_count:
                self.attempts.update_in_transaction(
                    transaction, run_id, uid,
                    {"warning_count": outcome.new_state.counted_incident_count, "updated_at": now.isoformat()},
                )
            return True

        return _txn(transaction)

    def resolve_block(
        self, *, run_id: str, uid: str, resolved_by: str, allow_continue: bool, notes: str, allowance: int = 0,
    ) -> None:
        """Explicit resolution only — never automatic, never resets the ledger, never reopens a FINALIZED attempt
        (contract: 'Unblock requires explicit resolution or an audited allowance; never reset the ledger or reopen
        final answers implicitly.'). Every decision is written to the ledger (as a reviewer entry, not a browser
        observation) and to the authorization audit, in the same transaction as the state change. `allowance` is the
        run's profile value: how many further counted departures are tolerated before the attempt blocks again."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> None:
            now = self._clock.now()
            attempt = self.attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is None:
                raise LookupError(f"no attempt for run={run_id} uid={uid}")
            if attempt.status != AttemptStatus.BLOCKED:
                raise AttemptNotBlockedError(f"attempt for uid={uid} is not currently blocked")
            state = self.states.get_in_transaction(transaction, run_id, uid) or _empty_state(run_id, uid, now)

            # "Uphold" makes no status change at all — the block simply stands. Only "allow to continue" actively
            # unblocks, and it keeps every count: blocking resumes once a further counted departure passes the floor.
            # Neither path jumps to FINALIZED (not a legal transition from BLOCKED) — an upheld block that outlives
            # the deadline is sealed by the ordinary finalization sweep like any other open attempt.
            if allow_continue:
                require_attempt_transition(attempt.status, AttemptStatus.IN_PROGRESS)
                self.attempts.update_in_transaction(
                    transaction, run_id, uid,
                    {"status": AttemptStatus.IN_PROGRESS.value, "version": attempt.version + 1, "updated_at": now.isoformat()},
                )
                self.states.upsert_in_transaction(
                    transaction,
                    state.model_copy(update={
                        "review_floor": state.counted_incident_count + allowance, "version": state.version + 1, "updated_at": now,
                    }),
                )

            decision = "allowed to continue" if allow_continue else "upheld — block stands"
            self.log.create_in_transaction(
                transaction,
                IncidentLogEntry(
                    id=uuid4().hex, run_id=run_id, uid=uid, kind=ObservationKind.PAGE_VISIBLE, counted=False,
                    client_timestamp=now, received_at=now, client_event_id=f"resolve-{uuid4().hex}",
                    note=f"reviewer decision: {decision}. {notes}".strip(), source="reviewer", actor_uid=resolved_by,
                ),
            )
            self._audit.append_in_transaction(transaction, build_audit_event(
                "exam_block_resolved", uid=uid, actor_uid=resolved_by,
                detail={"run_id": run_id, "allow_continue": allow_continue, "allowance": allowance if allow_continue else 0, "reason": notes[:500]},
                now=now,
            ))

        _txn(transaction)
