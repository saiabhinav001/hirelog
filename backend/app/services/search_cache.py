from __future__ import annotations

import importlib
import json
import logging
import threading
import time

from app.core.config import settings

try:
    redis = importlib.import_module("redis")
except Exception:  # pragma: no cover - redis is optional in some dev setups
    redis = None

logger = logging.getLogger(__name__)


class SearchCache:
    def __init__(self) -> None:
        self._ttl_seconds = max(30, int(settings.SEARCH_CACHE_TTL_SECONDS))
        self._max_entries = 500
        self._lock = threading.Lock()
        self._memory: dict[str, tuple[float, dict]] = {}
        self._redis = self._build_redis_client()

    def _build_redis_client(self):
        if not settings.SEARCH_REDIS_URL or redis is None:
            return None

        try:
            client = redis.Redis.from_url(settings.SEARCH_REDIS_URL, decode_responses=True)
            client.ping()
            logger.info("Search cache using Redis backend")
            return client
        except Exception:
            logger.exception("Redis cache unavailable; falling back to in-memory cache")
            return None

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    def _memory_get(self, key: str) -> dict | None:
        with self._lock:
            item = self._memory.get(key)
            if item and (time.time() - item[0]) < self._ttl_seconds:
                return item[1]
            self._memory.pop(key, None)
        return None

    def _memory_set(self, key: str, data: dict) -> None:
        with self._lock:
            if len(self._memory) >= self._max_entries:
                oldest = min(self._memory, key=lambda existing: self._memory[existing][0])
                self._memory.pop(oldest, None)
            self._memory[key] = (time.time(), data)

    def get(self, key: str) -> dict | None:
        if self._redis is not None:
            try:
                raw = self._redis.get(key)
                if raw:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        return parsed
            except Exception:
                logger.exception("Redis cache read failed for key=%s", key)

        return self._memory_get(key)

    def set(self, key: str, data: dict) -> None:
        if self._redis is not None:
            try:
                self._redis.setex(key, self._ttl_seconds, json.dumps(data))
            except Exception:
                logger.exception("Redis cache write failed for key=%s", key)

        self._memory_set(key, data)

    def clear(self) -> dict:
        memory_cleared = 0
        with self._lock:
            memory_cleared = len(self._memory)
            self._memory.clear()

        redis_cleared = 0
        if self._redis is not None:
            try:
                keys = self._redis.keys("search:*")
                if keys:
                    redis_cleared = self._redis.delete(*keys)
            except Exception:
                logger.exception("Redis cache clear failed")

        return {
            "backend": self.backend,
            "memory_entries_cleared": memory_cleared,
            "redis_entries_cleared": int(redis_cleared),
        }


search_cache = SearchCache()
