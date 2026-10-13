"""The in-process read cache: expiry, invalidation, single-flight loading and the guard against caching stale loads."""

from __future__ import annotations

import threading
import time

import pytest

from app.core import read_cache
from app.core.config import settings
from app.core.read_cache import TtlCache, cache_for


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _caches_on(monkeypatch):
    monkeypatch.setattr(settings, "EXAM_READ_CACHE_ENABLED", True)
    read_cache.clear_all()


def test_a_second_read_is_served_from_the_cache_until_it_expires() -> None:
    clock = _Clock()
    cache = TtlCache(ttl_seconds=5, clock=clock)
    loads: list[int] = []

    def load() -> str:
        loads.append(1)
        return "value"

    assert cache.get_or_load("k", load) == "value"
    assert cache.get_or_load("k", load) == "value"
    assert len(loads) == 1
    clock.now += 5.1
    assert cache.get_or_load("k", load) == "value"
    assert len(loads) == 2


def test_invalidate_forces_the_next_read_to_load() -> None:
    cache = TtlCache(ttl_seconds=60)
    loads: list[int] = []
    cache.get_or_load("k", lambda: loads.append(1) or "a")
    cache.invalidate("k")
    assert cache.get_or_load("k", lambda: loads.append(1) or "b") == "b"
    assert len(loads) == 2


def test_clear_drops_every_key() -> None:
    cache = TtlCache(ttl_seconds=60)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.clear()
    assert cache.peek("a") is None and cache.peek("b") is None


def test_none_is_not_cached_unless_asked() -> None:
    cache = TtlCache(ttl_seconds=60)
    loads: list[int] = []

    def load():
        loads.append(1)
        return None

    assert cache.get_or_load("missing", load) is None
    assert cache.get_or_load("missing", load) is None
    assert len(loads) == 2
    assert cache.get_or_load("kept", load, cache_none=True) is None
    assert cache.get_or_load("kept", load, cache_none=True) is None
    assert len(loads) == 3


def test_a_load_that_started_before_an_invalidation_is_not_stored() -> None:
    cache = TtlCache(ttl_seconds=60)
    started, release = threading.Event(), threading.Event()

    def slow_load() -> str:
        started.set()
        release.wait(timeout=5)
        return "stale"

    results: list[str] = []
    worker = threading.Thread(target=lambda: results.append(cache.get_or_load("k", slow_load)))
    worker.start()
    assert started.wait(timeout=5)
    cache.invalidate("k")  # a write lands while the old value is still being read
    release.set()
    worker.join(timeout=5)

    assert results == ["stale"]  # the caller that was already loading still gets what it read
    assert cache.get_or_load("k", lambda: "fresh") == "fresh"  # but the stale value was never cached


def test_a_write_through_put_fences_a_load_that_started_earlier() -> None:
    cache = TtlCache(ttl_seconds=60)
    started, release = threading.Event(), threading.Event()

    def slow_load() -> str:
        started.set()
        release.wait(timeout=5)
        return "old"

    worker = threading.Thread(target=lambda: cache.get_or_load("k", slow_load))
    worker.start()
    assert started.wait(timeout=5)
    cache.put("k", "written")
    release.set()
    worker.join(timeout=5)

    assert cache.peek("k") == "written"


def test_concurrent_misses_run_the_loader_once() -> None:
    cache = TtlCache(ttl_seconds=60)
    calls = {"n": 0}
    gate = threading.Event()

    def load() -> int:
        calls["n"] += 1
        gate.wait(timeout=5)
        return 7

    results: list[int] = []
    threads = [threading.Thread(target=lambda: results.append(cache.get_or_load("k", load))) for _ in range(8)]
    for thread in threads:
        thread.start()
    time.sleep(0.1)
    gate.set()
    for thread in threads:
        thread.join(timeout=5)

    assert results == [7] * 8
    assert calls["n"] == 1


def test_a_failing_loader_does_not_poison_the_key() -> None:
    cache = TtlCache(ttl_seconds=60)

    def boom() -> int:
        raise RuntimeError("firestore unavailable")

    with pytest.raises(RuntimeError):
        cache.get_or_load("k", boom)
    assert cache.get_or_load("k", lambda: 3) == 3


def test_the_cache_evicts_the_oldest_entry_past_its_size() -> None:
    cache = TtlCache(ttl_seconds=60, max_entries=2)
    for key in ("a", "b", "c"):
        cache.put(key, key)
    assert cache.peek("a") is None
    assert cache.peek("b") == "b" and cache.peek("c") == "c"


def test_the_switch_turns_every_cache_into_a_pass_through(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_READ_CACHE_ENABLED", False)
    cache = TtlCache(ttl_seconds=60)
    loads: list[int] = []
    cache.get_or_load("k", lambda: loads.append(1) or "v")
    cache.get_or_load("k", lambda: loads.append(1) or "v")
    assert len(loads) == 2
    cache.put("k", "x")
    assert cache.peek("k") is None


def test_each_database_object_gets_its_own_caches() -> None:
    class Db:
        pass

    first, second = Db(), Db()
    one = cache_for(first, "runs", ttl_seconds=60)
    assert cache_for(first, "runs", ttl_seconds=60) is one
    assert cache_for(second, "runs", ttl_seconds=60) is not one
    one.put("r", 1)
    assert cache_for(second, "runs", ttl_seconds=60).peek("r") is None


def test_an_object_that_cannot_be_weakly_referenced_is_never_shared() -> None:
    cache = cache_for(None, "runs", ttl_seconds=60)
    cache.put("r", 1)
    assert cache.peek("r") is None  # pass-through: placeholders in tests never leak state


def test_clear_all_empties_every_database() -> None:
    class Db:
        pass

    db = Db()
    cache = cache_for(db, "runs", ttl_seconds=60)
    cache.put("r", 1)
    read_cache.clear_all()
    assert cache.peek("r") is None
