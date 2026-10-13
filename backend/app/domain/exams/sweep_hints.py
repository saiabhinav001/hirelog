"""Admission hints for the finalization sweep, so it can sleep until the next deadline instead of scanning live attempts.

Advisory only: the sweep still re-reads and decides inside its own transactions.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from app.core import background

_lock = threading.Lock()
_woken_runs: set[str] = set()
_planned_deadline: Optional[datetime] = None  # earliest live deadline the sweep already plans to wake for


def note_attempt_started(run_id: str, deadline: Optional[datetime]) -> None:
    """A student was just admitted; wakes the sweep only if this deadline is earlier than the one it already sleeps until."""
    global _planned_deadline
    with _lock:
        _woken_runs.add(run_id)
        needs_wake = deadline is None or _planned_deadline is None or deadline < _planned_deadline
        if deadline is not None and needs_wake:
            _planned_deadline = deadline
    if needs_wake:
        background.wake("finalization")


def drain_woken_runs() -> set[str]:
    with _lock:
        woken = set(_woken_runs)
        _woken_runs.clear()
    return woken


def set_planned_deadline(deadline: Optional[datetime]) -> None:
    """The sweep reports the earliest live deadline it found (None when no attempt is live)."""
    global _planned_deadline
    with _lock:
        _planned_deadline = deadline


def reset() -> None:
    """Test hygiene."""
    global _planned_deadline
    with _lock:
        _woken_runs.clear()
        _planned_deadline = None
