"""One clean call-site for "publish this domain change to the live socket
layer" — combines sequencing + event construction + fan-out so route
handlers need one line, always called AFTER a transaction/service call has
already committed successfully.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from app.domain.exams.models import Attempt, ExamRun
from app.domain.live.connection_manager import LiveConnectionManager, live_connection_manager
from app.domain.live.events import attempt_status_changed_event, run_state_changed_event

_attempt_change_listeners: list[Callable[[str], None]] = []


def on_attempt_change(listener: Callable[[str], None]) -> None:
    """Register a callback (given the run id) for every committed attempt change, so
    read-side caches can drop what the change made stale."""
    _attempt_change_listeners.append(listener)


def publish_run_state_changed(run: ExamRun, *, now: datetime, manager: LiveConnectionManager = live_connection_manager) -> None:
    seq = manager.next_seq(run.id)
    manager.publish(run_state_changed_event(run, seq=seq, now=now))


def publish_attempt_status_changed(attempt: Attempt, *, now: datetime, manager: LiveConnectionManager = live_connection_manager) -> None:
    for listener in _attempt_change_listeners:
        listener(attempt.run_id)
    seq = manager.next_seq(attempt.run_id)
    manager.publish(attempt_status_changed_event(attempt, seq=seq, now=now))
