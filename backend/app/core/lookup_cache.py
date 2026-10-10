"""Small bounded in-process cache for look-ups of (near-)immutable documents.

On the free (Spark) tier every document read counts against a daily quota shared
with the rest of the project, so facts that never change during an exam (the
revision, an approved question variant, a run's enrolment) are read once and
served from memory. Failures (`LookupError`) are never cached.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional


class LookupCache:
    def __init__(self, max_entries: int, ttl_seconds: Optional[float] = None) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any, loader: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit is not None and (self._ttl is None or now - hit[0] < self._ttl):
                self._data.move_to_end(key)
                return hit[1]
        value = loader()
        with self._lock:
            self._data[key] = (now, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
        return value

    def invalidate(self, key: Any) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
