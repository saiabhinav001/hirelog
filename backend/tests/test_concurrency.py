"""Concurrency regression tests for the thread-safety fixes.

These exercise the exact code paths that previously had races: the sliding
window rate limiter, FAISS batched inserts under a shared lock, and the
coalescing dashboard-stats refresher.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import numpy as np
import pytest

from app.core.rate_limit import SlidingWindowLimiter


def _run_concurrently(target, n: int) -> None:
    threads = [threading.Thread(target=target) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_rate_limiter_admits_exactly_max_under_concurrency():
    """N concurrent hits on one key admit exactly `max_requests`, reject the rest."""
    limiter = SlidingWindowLimiter(10, 60, namespace="test_conc")
    admitted = {"n": 0}
    lock = threading.Lock()

    def hit():
        try:
            limiter.check("same-key")
        except Exception:  # HTTPException 429
            return
        with lock:
            admitted["n"] += 1

    _run_concurrently(hit, 100)
    # In-memory fallback (no Redis in CI): the window must admit exactly the cap.
    assert admitted["n"] == 10


def test_rate_limiter_isolates_distinct_keys():
    limiter = SlidingWindowLimiter(1, 60, namespace="test_iso")
    limiter.check("user-a")
    limiter.check("user-b")  # different key → must not be limited
    with pytest.raises(Exception):
        limiter.check("user-a")  # second hit on same key → limited


def test_faiss_batched_inserts_lose_no_vectors():
    """Concurrent add_vector calls must all land (no lost updates under the lock)."""
    from app.services.faiss_store import FaissStore

    tmp = Path(tempfile.mkdtemp())
    store = FaissStore(dimension=8)
    store.index_path = tmp / "index.faiss"
    store.mapping_path = tmp / "mapping.json"

    total = 200

    def add(i: int):
        store.add_vector(np.random.rand(8).astype("float32"), f"doc-{i}")

    threads = [threading.Thread(target=add, args=(i,)) for i in range(total)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    store.flush()
    assert len(store.mapping) == total
    assert len(set(store.mapping)) == total  # every doc id present exactly once


def test_dashboard_refresh_coalesces_concurrent_triggers():
    """Many concurrent refresh triggers collapse into a small number of runs."""
    import app.api.routes.dashboard as dash

    import time

    calls = {"n": 0}
    lock = threading.Lock()
    original = dash._do_stats_refresh

    def counting_refresh():
        with lock:
            calls["n"] += 1
        time.sleep(0.05)  # simulate scan latency so concurrent triggers overlap

    dash._do_stats_refresh = counting_refresh
    try:
        _run_concurrently(dash.update_dashboard_stats_async, 50)
    finally:
        dash._do_stats_refresh = original

    # 50 triggers must not cause 50 refreshes; coalescing keeps it tiny, but at
    # least one refresh must have run.
    assert 1 <= calls["n"] <= 5
