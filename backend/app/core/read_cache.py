"""Small in-process read cache for the exam module.

The free Firestore plan allows 50,000 document reads a day and most exam requests re-read the same few documents (the
run, its lockdown profile, the caller's memberships). Entries expire quickly and are dropped the moment this process
writes the underlying document, so a change made through this process is visible to the very next request.

One cache set lives per database object (a weak registry), so test fakes and the real client never share entries.
"""

from __future__ import annotations

import threading
import time
import weakref
from collections import OrderedDict
from typing import Any, Callable, Hashable, TypeVar

from app.core.config import settings

T = TypeVar("T")
_MISS = object()


class TtlCache:
    """Thread-safe TTL cache. `get_or_load` is single-flight (concurrent misses on one key run the loader once) and never
    stores a value loaded before an invalidation of that key, so a racing write cannot be overwritten by stale data."""

    def __init__(self, *, ttl_seconds: float, max_entries: int = 1024, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = float(ttl_seconds)
        self._max = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._data: "OrderedDict[Hashable, tuple[float, Any]]" = OrderedDict()
        self._seq = 0
        self._key_seq: dict[Hashable, int] = {}
        self._clear_seq = 0
        self._inflight: dict[Hashable, threading.Event] = {}

    def _active(self) -> bool:
        return self._ttl > 0 and bool(settings.EXAM_READ_CACHE_ENABLED)

    def _fresh(self, key: Hashable) -> Any:
        entry = self._data.get(key)
        if entry is None:
            return _MISS
        expires_at, value = entry
        if self._clock() >= expires_at:
            self._data.pop(key, None)
            return _MISS
        self._data.move_to_end(key)
        return value

    def _store(self, key: Hashable, value: Any) -> None:
        self._data[key] = (self._clock() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def _bump(self, key: Hashable | None) -> None:
        self._seq += 1
        if key is None:
            self._clear_seq = self._seq
            self._key_seq.clear()
            return
        self._key_seq[key] = self._seq
        if len(self._key_seq) > self._max * 4:  # forget old marks; treating it as a full clear is always safe
            self._clear_seq = self._seq
            self._key_seq.clear()

    def peek(self, key: Hashable, default: Any = None) -> Any:
        if not self._active():
            return default
        with self._lock:
            value = self._fresh(key)
        return default if value is _MISS else value

    def put(self, key: Hashable, value: Any) -> None:
        """Write-through: this process just made `value` true. Also fences any load that started earlier."""
        if not self._active():
            return
        with self._lock:
            self._bump(key)
            self._store(key, value)

    def invalidate(self, key: Hashable) -> None:
        with self._lock:
            self._bump(key)
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._bump(None)
            self._data.clear()

    def get_or_load(self, key: Hashable, loader: Callable[[], T], *, cache_none: bool = False) -> T:
        if not self._active():
            return loader()
        while True:
            with self._lock:
                value = self._fresh(key)
                if value is not _MISS:
                    return value
                event = self._inflight.get(key)
                leader = event is None
                if leader:
                    event = threading.Event()
                    self._inflight[key] = event
                    seq_at_start = self._seq
            if not leader:
                event.wait(timeout=30.0)
                continue
            try:
                loaded = loader()
                with self._lock:
                    unchanged = self._clear_seq <= seq_at_start and self._key_seq.get(key, 0) <= seq_at_start
                    if unchanged and (loaded is not None or cache_none):
                        self._store(key, loaded)
                return loaded
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                event.set()


_registry: "weakref.WeakKeyDictionary[Any, dict[str, TtlCache]]" = weakref.WeakKeyDictionary()
_registry_lock = threading.Lock()


def cache_for(db: Any, name: str, *, ttl_seconds: float, max_entries: int = 1024) -> TtlCache:
    """The named cache of this database object, created on first use (the TTL is fixed at creation). An object that
    cannot be weakly referenced (a placeholder in a test) gets a pass-through cache instead of a shared one."""
    with _registry_lock:
        try:
            caches = _registry.get(db)
            if caches is None:
                caches = {}
                _registry[db] = caches
        except TypeError:
            return TtlCache(ttl_seconds=0)
        cache = caches.get(name)
        if cache is None:
            cache = caches[name] = TtlCache(ttl_seconds=ttl_seconds, max_entries=max_entries)
        return cache


def clear_all() -> None:
    """Drops every cached entry of every database (operations switch and test hygiene)."""
    with _registry_lock:
        caches = [cache for per_db in _registry.values() for cache in per_db.values()]
    for cache in caches:
        cache.clear()
