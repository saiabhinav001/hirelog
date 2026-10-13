"""Server-side auto-finalization (Prompt 3, ADR 0019).

`ExamFinalizationSweeper` seals expired attempts in small batches - each
attempt in its OWN transaction, never one enormous one - and is safe to run
from several processes at once: every seal re-reads and re-decides inside its
transaction, so a duplicate or delayed sweep finds the attempt already sealed
(or still inside its accommodated/extended deadline) and does nothing.

Sealing is the same observable outcome as a student submit: the attempt gets a
`submit_receipt` over the last acknowledged drafts and an `attempt_finalized`
outbox row, which is what the grading handler consumes. (Before Prompt 3 the
sweeper was never started, set no receipt and emitted an `attempt_expired` row
nothing consumed, so expired attempts were never resolved or graded.)

With EXAM_SCHEDULE_AUTOMATION_ENABLED the same loop also performs scheduled
release/close through the ordinary `ExamCommandService.execute` path, so the
receipts, version checks and audit trail are identical to a faculty command.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from firebase_admin import firestore

from app.core.config import settings
from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.exams import sweep_hints
from app.domain.exams.clock import Clock, system_clock
from app.domain.exams.commands import SYSTEM_SCHEDULER_ACTOR, ExamCommandService
from app.domain.exams.decisions import decide_finalization_batch, decide_scheduled_command, decide_seal_on_expiry
from app.domain.exams.errors import ExamDomainError
from app.domain.exams.models import RunStatus
from app.domain.exams.repository import (
    AttemptRepository,
    DraftRepository,
    ExamRunRepository,
    OutboxRepository,
    new_outbox_entry,
)
from app.domain.exams.state_machine import require_attempt_transition

logger = logging.getLogger("hirelog.exams.finalization")

SWEEPER_ACTOR = "system:finalization-sweeper"


class ExamFinalizationSweeper:
    def __init__(self, db, clock: Clock = system_clock) -> None:
        self._db = db
        self._clock = clock
        self._runs = ExamRunRepository(db)
        self._attempts = AttemptRepository(db)
        self._drafts = DraftRepository(db)
        self._outbox = OutboxRepository(db)
        self._audit = AuthzAuditRepository(db)
        # What the last full sweep saw, so the worker can sleep until something can actually be sealed.
        self._tick_deadline: Optional[datetime] = None
        self._tick_backlog = False
        self._next_deadline: Optional[datetime] = None
        self._backlog = False
        # Runs found with no live attempt are not re-read until this monotonic time (an admission clears the entry).
        self._dormant_until: dict[str, float] = {}

    def seconds_until_next_deadline(self) -> Optional[float]:
        """Seconds until a sweep can next find something to seal: 0 while a backlog remains, None when no attempt is live."""
        if self._backlog:
            return 0.0
        if self._next_deadline is None:
            return None
        return max(0.0, (self._next_deadline - self._clock.now()).total_seconds()) + 1.0

    def _note_pending_deadlines(self, attempts: list, now: datetime) -> None:
        pending = [a.deadline for a in attempts if a.deadline is not None and a.deadline > now]
        if pending and (self._tick_deadline is None or min(pending) < self._tick_deadline):
            self._tick_deadline = min(pending)

    # -- sealing ---------------------------------------------------------------

    def sweep_once(self, run_id: str, *, batch_size: int) -> int:
        """One bounded pass over one run. Returns the number of attempts sealed
        this call - callers loop until this returns 0."""
        now = self._clock.now()
        candidates = self._attempts.list_in_progress(run_id) + self._attempts.list_blocked(run_id)
        dormant_for = float(settings.EXAM_FINALIZATION_DORMANT_RUN_SECONDS)
        if candidates or dormant_for <= 0:
            self._dormant_until.pop(run_id, None)
        else:
            self._dormant_until[run_id] = time.monotonic() + dormant_for
        # Only an attempt past its deadline can be sealed, so only those need their drafts read.
        expired = [attempt for attempt in candidates if attempt.deadline is not None and now > attempt.deadline]
        draft_versions_by_uid = {
            attempt.uid: self._drafts.all_versions(run_id, attempt.uid) for attempt in expired
        }
        self._note_pending_deadlines(candidates, now)
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
        if batch.skipped or sealed < len(batch.sealed):
            self._tick_backlog = True  # more to seal than this pass handled, or a seal lost a race: look again soon
        return sealed

    def sweep_active_runs_once(self, *, batch_size: int) -> int:
        """One tick over every run that can still hold unsealed attempts:
        released/paused/closed runs plus recently ended ones (a hard stop must
        not strand anyone). Bounded: at most `batch_size` seals per run."""
        now = self._clock.now()
        runs = {
            run.id: run
            for run in self._runs.list_by_statuses([RunStatus.RELEASED, RunStatus.PAUSED, RunStatus.CLOSED])
        }
        window = timedelta(hours=settings.EXAM_FINALIZATION_ENDED_RUN_WINDOW_HOURS)
        for run in self._runs.list_ended_since(now - window):
            runs.setdefault(run.id, run)

        total = 0
        self._tick_deadline = None
        self._tick_backlog = False
        for run_id in sweep_hints.drain_woken_runs():
            self._dormant_until.pop(run_id, None)
        monotonic_now = time.monotonic()
        for run_id in runs:
            if self._dormant_until.get(run_id, 0.0) > monotonic_now:
                continue  # nothing was live the last time this run was read, and nobody has been admitted since
            try:
                total += self.sweep_once(run_id, batch_size=batch_size)
            except Exception:  # noqa: BLE001 - one bad run must not stop the sweep
                logger.exception("finalization sweep failed for run %s", run_id)
                self._tick_backlog = True
        for run_id in [known for known in self._dormant_until if known not in runs]:
            del self._dormant_until[run_id]
        self._next_deadline, self._backlog = self._tick_deadline, self._tick_backlog
        sweep_hints.set_planned_deadline(self._next_deadline)
        return total

    def _seal_one(self, run_id: str, uid: str) -> bool:
        """Re-decides fresh inside its own transaction - the batch-level
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

            versions = self._drafts.all_versions_in_transaction(transaction, run_id, uid)
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
                    "submit_receipt": decision.receipt.model_dump(mode="json"),
                    "finalization_reason": "deadline_expired",
                    "finalized_by": SWEEPER_ACTOR,
                    "updated_at": now.isoformat(),
                },
            )
            # The grading handler consumes `attempt_finalized`; `attempt_expired`
            # stays for any observer that distinguishes the cause.
            self._outbox.add_in_transaction(
                transaction,
                new_outbox_entry(
                    kind="attempt_finalized",
                    run_id=run_id,
                    payload={"uid": uid, "receipt_id": decision.receipt.receipt_id, "reason": "deadline_expired"},
                    now=now,
                ),
            )
            self._outbox.add_in_transaction(
                transaction, new_outbox_entry(kind="attempt_expired", run_id=run_id, payload={"uid": uid}, now=now)
            )
            self._audit.append_in_transaction(
                transaction,
                build_audit_event(
                    "exam_attempt_expired", uid=uid, actor_uid=SWEEPER_ACTOR,
                    detail={
                        "run_id": run_id, "from_status": attempt.status.value,
                        "deadline": attempt.deadline.isoformat() if attempt.deadline else None,
                        "receipt_id": decision.receipt.receipt_id,
                    },
                    now=now,
                ),
            )
            return True

        sealed = _txn(transaction)
        if sealed:
            self._publish_attempt(run_id, uid)
        return sealed

    def _publish_attempt(self, run_id: str, uid: str) -> None:
        try:
            from app.domain.live.publisher import publish_attempt_status_changed

            attempt = self._attempts.get(run_id, uid)
            if attempt is not None:
                publish_attempt_status_changed(attempt, now=self._clock.now())
        except Exception:  # noqa: BLE001 - live fan-out is best effort, the seal already committed
            logger.warning("live publish after seal failed for run=%s uid=%s", run_id, uid, exc_info=True)

    # -- schedule automation -----------------------------------------------------

    def run_schedule_automation_once(self) -> int:
        """Release PUBLISHED runs whose window has opened and close
        RELEASED/PAUSED runs whose window has ended. Returns commands applied.

        Uses deterministic idempotency keys and the run's CURRENT version, so
        two processes (or a faculty member acting at the same moment) cannot
        double-apply: the loser sees a version conflict / illegal transition
        and simply skips until the next tick. `close` is forced because it
        only stops NEW admissions - admitted students keep their own
        (accommodated) deadlines."""
        now = self._clock.now()
        service = ExamCommandService(self._db, clock=self._clock)
        runs = self._runs.list_by_statuses([RunStatus.PUBLISHED, RunStatus.RELEASED, RunStatus.PAUSED])
        applied = 0
        for run in runs:
            command: Optional[str] = decide_scheduled_command(run=run, now=now)
            if command is None:
                continue
            try:
                outcome = service.execute(
                    run_id=run.id, command=command, expected_version=run.version,
                    idempotency_key=f"schedule-{command}", force=True,
                    actor_uid=SYSTEM_SCHEDULER_ACTOR, reason="scheduled window",
                )
            except ExamDomainError as exc:
                logger.info("scheduled %s skipped for run %s: %s", command, run.id, exc)
                continue
            except Exception:  # noqa: BLE001
                logger.exception("scheduled %s failed for run %s", command, run.id)
                continue
            if outcome.applied:
                applied += 1
                self._publish_run(run.id)
        return applied

    def _publish_run(self, run_id: str) -> None:
        try:
            from app.domain.live.publisher import publish_run_state_changed

            run = self._runs.get(run_id)
            if run is not None:
                publish_run_state_changed(run, now=self._clock.now())
        except Exception:  # noqa: BLE001
            logger.warning("live publish after scheduled command failed for run=%s", run_id, exc_info=True)
