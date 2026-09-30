"""In-process WebSocket connection registry: per-run monotonic sequencing,
scoped fan-out (broadcast vs. one student + faculty room), and a thread-safe
publish path so synchronous route handlers (the existing command/student
services are all sync) can enqueue events without touching the asyncio loop
directly — each connection's own async task drains its queue and sends.

Single-process only (documented limitation, same class as the outbox/rate-
limiter patterns already in this codebase): a second server instance has its
own registry and never sees another instance's connections. Horizontal
scaling needs a shared pub/sub backend (e.g. Redis), not implemented here.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field

from app.domain.live.models import LiveEvent, LiveRole

_MAX_QUEUED_EVENTS_PER_CONNECTION = 200


@dataclass
class Connection:
    """One admitted socket. `queue` is a stdlib thread-safe queue — safe to
    `put` from any thread, drained by the connection's own async task."""

    connection_id: str
    run_id: str
    role: LiveRole
    uid: str
    queue: "queue.Queue[LiveEvent]" = field(default_factory=lambda: queue.Queue(maxsize=_MAX_QUEUED_EVENTS_PER_CONNECTION))


class LiveConnectionManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: dict[str, list[Connection]] = {}
        self._seq_counters: dict[str, int] = {}

    def register(self, connection: Connection) -> None:
        with self._lock:
            self._connections.setdefault(connection.run_id, []).append(connection)

    def unregister(self, connection: Connection) -> None:
        with self._lock:
            conns = self._connections.get(connection.run_id)
            if conns and connection in conns:
                conns.remove(connection)
                if not conns:
                    del self._connections[connection.run_id]

    def connection_count(self, run_id: str) -> int:
        with self._lock:
            return len(self._connections.get(run_id, []))

    def next_seq(self, run_id: str) -> int:
        with self._lock:
            current = self._seq_counters.get(run_id, 0) + 1
            self._seq_counters[run_id] = current
            return current

    def current_seq(self, run_id: str) -> int:
        with self._lock:
            return self._seq_counters.get(run_id, 0)

    def publish(self, event: LiveEvent) -> int:
        """Thread-safe — callable directly from sync route handlers, always
        AFTER their transaction has committed (never inside one, per
        contract: 'No external service calls... inside transaction
        callbacks'). Returns how many connections actually received it.
        A full per-connection queue silently drops the event for that
        connection rather than blocking the publisher — a client that far
        behind will detect the resulting seq gap and reconcile over REST."""
        with self._lock:
            conns = list(self._connections.get(event.run_id, []))
        delivered = 0
        for conn in conns:
            if event.target_uid is not None and conn.role == LiveRole.STUDENT and conn.uid != event.target_uid:
                continue  # scoped event — not this student's own
            try:
                conn.queue.put_nowait(event)
                delivered += 1
            except queue.Full:
                pass
        return delivered


live_connection_manager = LiveConnectionManager()
