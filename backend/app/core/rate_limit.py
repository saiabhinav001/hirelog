from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status


class SlidingWindowLimiter:
    """Thread-safe in-memory sliding-window limiter.

    This protects expensive endpoints from burst traffic on free-tier hosting.
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max(1, int(max_requests))
        self.window_seconds = max(1, int(window_seconds))
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, entries: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while entries and entries[0] <= cutoff:
            entries.popleft()

    def check(self, key: str, *, detail: str = "Too many requests.") -> None:
        now = time.monotonic()
        with self._lock:
            entries = self._events[key]
            self._prune(entries, now)

            if len(entries) >= self.max_requests:
                retry_after = max(1, int(self.window_seconds - (now - entries[0])))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=detail,
                    headers={"Retry-After": str(retry_after)},
                )

            entries.append(now)

            # Keep memory bounded when many unique keys appear.
            if len(self._events) > 10_000:
                stale_cutoff = now - (self.window_seconds * 2)
                stale_keys = [
                    item_key
                    for item_key, item_entries in self._events.items()
                    if not item_entries or item_entries[-1] < stale_cutoff
                ]
                for stale_key in stale_keys[:2_000]:
                    self._events.pop(stale_key, None)


def client_identifier(request: Request, user_key: str | None = None) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    client_host = request.client.host if request.client else "unknown"
    ip = forwarded_for or client_host
    if user_key:
        return f"{user_key}:{ip}"
    return ip
