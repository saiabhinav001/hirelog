"""What in-process events tell the finalization sweep, so it can stay asleep instead of re-reading attempts.

A run with no live attempt is not re-read for a long time, and an admission names the new attempt's deadline, so the sweep
sleeps until the earliest known deadline rather than scanning every live attempt on a timer. Everything here is advisory: the
sweep still re-reads and re-decides inside its own transactions, and it still ticks at its idle ceiling as a safety net.
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
    """A student was just admitted: the run has a live attempt again. Wakes the sweep only when this deadline is earlier than
    the one it already sleeps until (admissions of one run arrive in deadline order, so the first one is the one that matters)."""
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
