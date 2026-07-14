from __future__ import annotations

import importlib
import logging
import threading
import time
import uuid
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from app.core.config import settings

try:
    redis = importlib.import_module("redis")
except Exception:  # pragma: no cover - redis is optional in some dev setups
    redis = None

logger = logging.getLogger(__name__)

# Shared Redis client (one connection pool for all limiters). Built lazily so a
# missing/unavailable Redis never blocks startup — limiters transparently fall
# back to the per-process in-memory window.
_redis_client = None
_redis_init_lock = threading.Lock()
_redis_initialised = False


def _get_redis():
    global _redis_client, _redis_initialised
    if _redis_initialised:
        return _redis_client
    with _redis_init_lock:
        if _redis_initialised:
            return _redis_client
        _redis_initialised = True
        if not settings.SEARCH_REDIS_URL or redis is None:
            _redis_client = None
            return None
        try:
            client = redis.Redis.from_url(settings.SEARCH_REDIS_URL)
            client.ping()
            logger.info("Rate limiter using Redis backend (distributed)")
            _redis_client = client
        except Exception:
            logger.exception("Redis unavailable for rate limiting; using in-memory fallback")
            _redis_client = None
        return _redis_client


class SlidingWindowLimiter:
    """Sliding-window limiter with a Redis backend and in-memory fallback.

    When ``SEARCH_REDIS_URL`` is configured the window lives in Redis (a sorted
    set per client key), so limits are shared across processes and survive
    restarts / rolling deploys / autoscaling. Without Redis — or if a Redis call
    fails — it degrades to a thread-safe per-process in-memory window so the
    endpoint is still protected on single-instance / dev setups.

    ``namespace`` must be stable and distinct per logical limiter so that the
    mutation, dashboard and search limiters don't share counters in Redis.
    """

    def __init__(
        self, max_requests: int, window_seconds: int, *, namespace: str = "default"
    ) -> None:
        self.max_requests = max(1, int(max_requests))
        self.window_seconds = max(1, int(window_seconds))
        self.namespace = namespace
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    # ── Public API ───────────────────────────────────────────────────────────
    def check(self, key: str, *, detail: str = "Too many requests.") -> None:
        client = _get_redis()
        if client is not None:
            try:
                self._check_redis(client, key, detail)
                return
            except HTTPException:
                raise
            except Exception:
                # Any Redis/transport error → fail open to the in-memory window
                # rather than failing the request.
                logger.exception("Redis rate-limit check failed; using in-memory fallback")
        self._check_memory(key, detail)

    # ── Redis backend ────────────────────────────────────────────────────────
    def _check_redis(self, client, key: str, detail: str) -> None:
        redis_key = f"rl:{self.namespace}:{key}"
        now = time.time()
        window = self.window_seconds
        member = f"{now:.6f}:{uuid.uuid4().hex}"

        pipe = client.pipeline()
        pipe.zremrangebyscore(redis_key, 0, now - window)
        pipe.zadd(redis_key, {member: now})
        pipe.zcard(redis_key)
        pipe.expire(redis_key, window)
        results = pipe.execute()
        count = int(results[2])

        if count > self.max_requests:
            # Don't let a rejected request count against the window (mirrors the
            # in-memory limiter, which records only admitted requests).
            try:
                client.zrem(redis_key, member)
            except Exception:
                pass
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=detail,
                headers={"Retry-After": str(self._redis_retry_after(client, redis_key, now))},
            )

    def _redis_retry_after(self, client, redis_key: str, now: float) -> int:
        try:
            oldest = client.zrange(redis_key, 0, 0, withscores=True)
            if oldest:
                oldest_score = float(oldest[0][1])
                return max(1, int(self.window_seconds - (now - oldest_score)))
        except Exception:
            pass
        return self.window_seconds

    # ── In-memory fallback ───────────────────────────────────────────────────
    def _prune(self, entries: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while entries and entries[0] <= cutoff:
            entries.popleft()

    def _check_memory(self, key: str, detail: str) -> None:
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
