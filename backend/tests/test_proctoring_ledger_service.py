"""`ProctoringLedgerService` integration tests: end-to-end record_observation
(threshold -> BLOCKED transition, idempotent re-block), resolve_block
(uphold vs allow-continue, not-blocked guard), and the stale-incident sweep
— all against in-memory fakes (same `Service.__new__` + fake-repo pattern
established in `test_institution_auth_service.py`).

`firestore.transactional` is patched to a plain passthrough (same as that
file) since the fake `transaction` object here has none of the real retry-
on-contention machinery — this proves the ledger LOGIC, not true concurrent-
writer transactional correctness (same documented limitation as everywhere
else in this codebase).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.exams.clock import FixedClock
from app.domain.exams.models import Attempt, AttemptStatus, ExamRun, RunStatus
from app.domain.exams.policy import ExamPolicy
from app.domain.proctoring import ledger_service as ledger_service_module
from app.domain.proctoring.errors import AttemptNotBlockedError
from app.domain.proctoring.ledger_service import ProctoringLedgerService
from app.domain.proctoring.models import IncidentLogEntry, ObservationKind, ProctoringState

NOW = FixedClock().now()
GRACE = 2.0
COOLDOWN = 5.0


@pytest.fixture(autouse=True)
def _passthrough_transactions(monkeypatch):
    monkeypatch.setattr(ledger_service_module.firestore, "transactional", lambda fn: fn)


class _FakeStateRepo:
    def __init__(self) -> None:
        self.store: dict[str, ProctoringState] = {}

    def _key(self, run_id, uid):
        return f"{run_id}__{uid}"

    def get_in_transaction(self, _txn, run_id, uid):
        return self.store.get(self._key(run_id, uid))

    def upsert_in_transaction(self, _txn, state):
        self.store[self._key(state.run_id, state.uid)] = state

    def get(self, run_id, uid):
        return self.store.get(self._key(run_id, uid))

    def list_open_incidents(self, *, limit):
        return [s for s in self.store.values() if s.open_incident_started_at is not None][:limit]


class _FakeLogRepo:
    def __init__(self) -> None:
        self.entries: list[IncidentLogEntry] = []

    def create_in_transaction(self, _txn, entry):
        self.entries.append(entry)

    def list_for_attempt(self, run_id, uid):
        return [e for e in self.entries if e.run_id == run_id and e.uid == uid]


class _FakeAttemptRepo:
    def __init__(self, attempts: dict[str, Attempt]) -> None:
        self.store = attempts

    def _key(self, run_id, uid):
        return f"{run_id}__{uid}"

    def get_in_transaction(self, _txn, run_id, uid):
        return self.store.get(self._key(run_id, uid))

    def get(self, run_id, uid):
        return self.store.get(self._key(run_id, uid))

    def update_in_transaction(self, _txn, run_id, uid, update):
        current = self.store[self._key(run_id, uid)]
        self.store[self._key(run_id, uid)] = current.model_copy(update=update)


class _FakeRunRepo:
    def __init__(self, run: ExamRun) -> None:
        self._run = run

    def get(self, run_id):
        return self._run if run_id == self._run.id else None


class _FakeDb:
    def transaction(self):
        return object()


def _run(threshold: int = 3) -> ExamRun:
    return ExamRun(
        id="run-1", definition_id="def-1", revision_id="rev-1", status=RunStatus.RELEASED,
        policy=ExamPolicy(duration_minutes=60, warning_threshold=threshold),
        enrolled_uids=["u1"], created_at=NOW, updated_at=NOW,
    )


def _attempt(status: AttemptStatus = AttemptStatus.IN_PROGRESS, **overrides) -> Attempt:
    defaults = dict(run_id="run-1", uid="u1", status=status, version=1, epoch=1, deadline=NOW + timedelta(hours=1))
    defaults.update(overrides)
    return Attempt(**defaults)


def _service(*, run: ExamRun, attempts: dict[str, Attempt]) -> ProctoringLedgerService:
    service = ProctoringLedgerService.__new__(ProctoringLedgerService)
    service._db = _FakeDb()
    service._clock = FixedClock(NOW)
    service.states = _FakeStateRepo()
    service.log = _FakeLogRepo()
    service.attempts = _FakeAttemptRepo(attempts)
    service.runs = _FakeRunRepo(run)
    return service


def _record(service, *, kind, event_id, at):
    return service.record_observation(
        run_id="run-1", uid="u1", kind=kind, client_event_id=event_id, client_timestamp=at,
        grace_seconds=GRACE, cooldown_seconds=COOLDOWN, threshold=service.runs.get("run-1").policy.warning_threshold,
    )


def test_threshold_crossing_transitions_attempt_to_blocked() -> None:
    run = _run(threshold=2)
    attempt = _attempt()
    service = _service(run=run, attempts={"run-1__u1": attempt})

    for i in range(2):
        _record(service, kind=ObservationKind.PAGE_HIDDEN, event_id=f"open-{i}", at=NOW + timedelta(seconds=i * 100))
        _record(service, kind=ObservationKind.PAGE_VISIBLE, event_id=f"close-{i}", at=NOW + timedelta(seconds=i * 100 + 3))

    updated = service.attempts.get("run-1", "u1")
    assert updated.status == AttemptStatus.BLOCKED
    assert updated.warning_count == 2


def test_blocking_is_idempotent_does_not_raise_on_repeat_departures() -> None:
    run = _run(threshold=1)
    attempt = _attempt()
    service = _service(run=run, attempts={"run-1__u1": attempt})

    _record(service, kind=ObservationKind.PAGE_HIDDEN, event_id="open-0", at=NOW)
    _record(service, kind=ObservationKind.PAGE_VISIBLE, event_id="close-0", at=NOW + timedelta(seconds=3))
    assert service.attempts.get("run-1", "u1").status == AttemptStatus.BLOCKED

    # A further departure after already-blocked must not raise (no illegal
    # BLOCKED->BLOCKED transition attempted) and must not re-count oddly.
    _record(service, kind=ObservationKind.PAGE_HIDDEN, event_id="open-1", at=NOW + timedelta(seconds=50))
    outcome = _record(service, kind=ObservationKind.PAGE_VISIBLE, event_id="close-1", at=NOW + timedelta(seconds=53))
    assert outcome.action == "incident_closed_counted"
    assert service.attempts.get("run-1", "u1").status == AttemptStatus.BLOCKED


def test_replaying_the_same_event_id_does_not_change_warning_count() -> None:
    run = _run(threshold=5)
    attempt = _attempt()
    service = _service(run=run, attempts={"run-1__u1": attempt})

    _record(service, kind=ObservationKind.PAGE_HIDDEN, event_id="open-0", at=NOW)
    _record(service, kind=ObservationKind.PAGE_VISIBLE, event_id="close-0", at=NOW + timedelta(seconds=3))
    assert service.attempts.get("run-1", "u1").warning_count == 1

    # Offline replay: the SAME close event resent.
    _record(service, kind=ObservationKind.PAGE_VISIBLE, event_id="close-0", at=NOW + timedelta(seconds=3))
    assert service.attempts.get("run-1", "u1").warning_count == 1


def test_resolve_block_allow_continue_resumes_in_progress() -> None:
    run = _run()
    attempt = _attempt(status=AttemptStatus.BLOCKED, warning_count=3)
    service = _service(run=run, attempts={"run-1__u1": attempt})

    service.resolve_block(run_id="run-1", uid="u1", resolved_by="fac-1", allow_continue=True, notes="reviewed, false alarm")

    assert service.attempts.get("run-1", "u1").status == AttemptStatus.IN_PROGRESS
    log = service.log.list_for_attempt("run-1", "u1")
    assert any("fac-1" in e.note for e in log)


def test_resolve_block_uphold_leaves_status_blocked() -> None:
    run = _run()
    attempt = _attempt(status=AttemptStatus.BLOCKED, warning_count=3)
    service = _service(run=run, attempts={"run-1__u1": attempt})

    service.resolve_block(run_id="run-1", uid="u1", resolved_by="fac-1", allow_continue=False, notes="upheld")

    assert service.attempts.get("run-1", "u1").status == AttemptStatus.BLOCKED  # block stands


def test_resolve_block_on_non_blocked_attempt_raises() -> None:
    run = _run()
    attempt = _attempt(status=AttemptStatus.IN_PROGRESS)
    service = _service(run=run, attempts={"run-1__u1": attempt})

    with pytest.raises(AttemptNotBlockedError):
        service.resolve_block(run_id="run-1", uid="u1", resolved_by="fac-1", allow_continue=True, notes="")


def test_sweep_stale_incidents_closes_and_blocks_when_threshold_reached() -> None:
    run = _run(threshold=1)
    attempt = _attempt()
    service = _service(run=run, attempts={"run-1__u1": attempt})

    _record(service, kind=ObservationKind.PAGE_HIDDEN, event_id="open-0", at=NOW)  # never closed — student closed the tab
    assert service.states.get("run-1", "u1").open_incident_started_at == NOW

    # Not yet stale relative to "now" — nothing to close.
    not_yet = service.sweep_stale_incidents_once(grace_seconds=GRACE, stale_after_seconds=120)
    assert not_yet == 0

    # Advance the clock well past the staleness window.
    service._clock = FixedClock(NOW + timedelta(seconds=200))
    closed_count = service.sweep_stale_incidents_once(grace_seconds=GRACE, stale_after_seconds=120)

    assert closed_count == 1
    assert service.attempts.get("run-1", "u1").status == AttemptStatus.BLOCKED
