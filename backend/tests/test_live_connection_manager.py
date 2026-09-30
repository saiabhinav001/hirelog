"""`LiveConnectionManager` tests: per-run monotonic sequencing, scoped
fan-out (broadcast vs. one student + faculty room), queue-full drop
behavior, and multi-run isolation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.live.connection_manager import Connection, LiveConnectionManager
from app.domain.live.models import LiveEvent, LiveEventKind, LiveRole

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _event(run_id: str, seq: int, *, target_uid: str | None = None) -> LiveEvent:
    return LiveEvent(seq=seq, kind=LiveEventKind.RUN_STATE_CHANGED, run_id=run_id, state_version=1, target_uid=target_uid, created_at=NOW)


def test_next_seq_is_monotonic_per_run() -> None:
    manager = LiveConnectionManager()
    assert manager.next_seq("run-1") == 1
    assert manager.next_seq("run-1") == 2
    assert manager.next_seq("run-1") == 3


def test_seq_counters_are_isolated_per_run() -> None:
    manager = LiveConnectionManager()
    manager.next_seq("run-1")
    manager.next_seq("run-1")
    assert manager.next_seq("run-2") == 1


def test_register_and_unregister_tracks_connection_count() -> None:
    manager = LiveConnectionManager()
    conn = Connection(connection_id="c1", run_id="run-1", role=LiveRole.STUDENT, uid="u1")
    manager.register(conn)
    assert manager.connection_count("run-1") == 1
    manager.unregister(conn)
    assert manager.connection_count("run-1") == 0


def test_broadcast_event_reaches_every_connection_in_the_run() -> None:
    manager = LiveConnectionManager()
    faculty = Connection(connection_id="c1", run_id="run-1", role=LiveRole.FACULTY, uid="fac-1")
    student_a = Connection(connection_id="c2", run_id="run-1", role=LiveRole.STUDENT, uid="u1")
    student_b = Connection(connection_id="c3", run_id="run-1", role=LiveRole.STUDENT, uid="u2")
    for conn in (faculty, student_a, student_b):
        manager.register(conn)

    delivered = manager.publish(_event("run-1", 1, target_uid=None))

    assert delivered == 3
    for conn in (faculty, student_a, student_b):
        assert conn.queue.get_nowait().seq == 1


def test_targeted_event_reaches_only_faculty_and_that_students_own_connection() -> None:
    manager = LiveConnectionManager()
    faculty = Connection(connection_id="c1", run_id="run-1", role=LiveRole.FACULTY, uid="fac-1")
    student_a = Connection(connection_id="c2", run_id="run-1", role=LiveRole.STUDENT, uid="u1")
    student_b = Connection(connection_id="c3", run_id="run-1", role=LiveRole.STUDENT, uid="u2")
    for conn in (faculty, student_a, student_b):
        manager.register(conn)

    delivered = manager.publish(_event("run-1", 1, target_uid="u1"))

    assert delivered == 2  # faculty + student_a only
    assert faculty.queue.get_nowait().seq == 1
    assert student_a.queue.get_nowait().seq == 1
    assert student_b.queue.empty()


def test_event_never_reaches_connections_on_a_different_run() -> None:
    manager = LiveConnectionManager()
    other_run_conn = Connection(connection_id="c1", run_id="run-OTHER", role=LiveRole.STUDENT, uid="u1")
    manager.register(other_run_conn)

    delivered = manager.publish(_event("run-1", 1, target_uid=None))

    assert delivered == 0
    assert other_run_conn.queue.empty()


def test_full_queue_drops_the_event_for_that_connection_without_raising() -> None:
    manager = LiveConnectionManager()
    conn = Connection(connection_id="c1", run_id="run-1", role=LiveRole.FACULTY, uid="fac-1")
    manager.register(conn)
    for i in range(300):  # exceeds the connection's bounded queue size
        manager.publish(_event("run-1", i, target_uid=None))

    assert conn.queue.full()  # saturated, but publish() never raised/blocked
