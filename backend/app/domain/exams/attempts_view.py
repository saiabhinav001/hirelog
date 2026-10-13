"""In-memory candidate grid for the faculty control room.

Every live event makes each open control room reload the grid, and one page read costs a Firestore read per attempt, so a
cohort starting or finishing multiplies into thousands of reads. This keeps one snapshot per run and re-reads only the rows
the live publisher reports as changed (`mark_dirty`, called after the change committed). A full re-read happens at most every
EXAM_ATTEMPT_VIEW_RESYNC_SECONDS as a safety net for a change that was never published; runs too large to hold fall back to
the paged read.
"""

from __future__ import annotations

import threading
import time
import weakref
from typing import Any, Optional

from app.core.config import settings
from app.domain.exams.models import Attempt
from app.domain.exams.repository import AttemptRepository

_MAX_ROWS = 500


class _RunRows:
    def __init__(self) -> None:
        self.lock = threading.Lock()  # guards rows/loaded_at/overflow and serialises reloads of this run
        self.dirty_lock = threading.Lock()  # guards `dirty` only, so a publisher never waits behind a Firestore read
        self.rows: dict[str, Attempt] = {}
        self.dirty: set[str] = set()
        self.loaded_at: Optional[float] = None
        self.overflow = False


_views: "weakref.WeakKeyDictionary[Any, dict[str, _RunRows]]" = weakref.WeakKeyDictionary()
_views_lock = threading.Lock()


def _state_for(db: Any, run_id: str) -> _RunRows:
    with _views_lock:
        per_db = _views.get(db)
        if per_db is None:
            per_db = {}
            _views[db] = per_db
        state = per_db.get(run_id)
        if state is None:
            state = per_db[run_id] = _RunRows()
        return state


def mark_dirty(run_id: str, uid: str) -> None:
    """An attempt of `run_id` changed (and committed): its row is re-read on the next grid request."""
    with _views_lock:
        states = [per_db[run_id] for per_db in _views.values() if run_id in per_db]
    for state in states:
        with state.dirty_lock:
            state.dirty.add(uid)


def clear_all() -> None:
    with _views_lock:
        _views.clear()


def _slice(rows: dict[str, Attempt], limit: int, cursor_uid: Optional[str]) -> tuple[list[Attempt], Optional[str]]:
    ordered = sorted(rows)
    if cursor_uid:
        ordered = [uid for uid in ordered if uid > cursor_uid]
    window = [rows[uid] for uid in ordered[: limit + 1]]
    attempts = window[:limit]
    return attempts, (attempts[-1].uid if len(window) > limit else None)


def page_for_run(db: Any, run_id: str, *, limit: int, cursor_uid: Optional[str] = None) -> tuple[list[Attempt], Optional[str]]:
    """Same contract as `AttemptRepository.list_page_for_run`, served from the in-memory snapshot when it is enabled."""
    repo = AttemptRepository(db)
    resync = float(settings.EXAM_ATTEMPT_VIEW_RESYNC_SECONDS)
    if not settings.EXAM_READ_CACHE_ENABLED or resync <= 0:
        return repo.list_page_for_run(run_id, limit=limit, cursor_uid=cursor_uid)

    state = _state_for(db, run_id)
    with state.lock:
        now = time.monotonic()
        stale = state.loaded_at is None or now - state.loaded_at >= resync
        if stale:
            with state.dirty_lock:
                state.dirty.clear()  # anything marked so far committed before this read, which will include it
            rows = repo.list_all_for_run(run_id, cap=_MAX_ROWS + 1)
            state.loaded_at = time.monotonic()
            state.overflow = len(rows) > _MAX_ROWS
            state.rows = {} if state.overflow else {attempt.uid: attempt for attempt in rows}
        elif not state.overflow:
            with state.dirty_lock:
                changed = list(state.dirty)
                state.dirty.clear()
            if changed:
                fresh = repo.get_many(run_id, changed)
                for uid in changed:
                    if uid in fresh:
                        state.rows[uid] = fresh[uid]
                    else:
                        state.rows.pop(uid, None)
        if state.overflow:
            return repo.list_page_for_run(run_id, limit=limit, cursor_uid=cursor_uid)
        return _slice(state.rows, limit, cursor_uid)
