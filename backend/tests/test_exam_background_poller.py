"""The exam workers back off while idle (the free Firestore plan allows 50,000 reads a day) and wake on work."""

from __future__ import annotations

import threading
import time

from app.core import background
from app.core.background import AdaptivePoller


def _poller(tick, **kwargs) -> AdaptivePoller:
    kwargs.setdefault("min_interval", 2.0)
    kwargs.setdefault("max_interval", 60.0)
    return AdaptivePoller("t", tick, clock=lambda: 0.0, **kwargs)


def test_an_idle_worker_backs_off_to_its_maximum() -> None:
    poller = _poller(lambda: 0)
    interval, seen = 2.0, []
    for _ in range(8):
        interval = poller.run_once(interval)
        seen.append(interval)
    assert seen == [4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0]


def test_finding_work_returns_to_the_fast_cadence() -> None:
    results = iter([0, 0, 3, 0])
    poller = _poller(lambda: next(results))
    interval, seen = 2.0, []
    for _ in range(4):
        interval = poller.run_once(interval)
        seen.append(interval)
    assert seen == [4.0, 8.0, 2.0, 4.0]


def test_a_wake_keeps_the_fast_cadence_for_the_burst_window_only() -> None:
    now = [0.0]
    poller = AdaptivePoller("t", lambda: 0, min_interval=2.0, max_interval=60.0, burst_seconds=30.0, clock=lambda: now[0])
    poller.wake()
    assert poller.next_interval(32.0, False) == 2.0
    now[0] = 31.0
    assert poller.next_interval(2.0, False) == 4.0


def test_a_failing_tick_never_stops_the_loop() -> None:
    def boom() -> int:
        raise RuntimeError("datastore down")

    poller = _poller(boom, max_interval=8.0)
    assert poller.run_once(2.0) == 4.0
    assert poller.ticks == 1


def test_the_hint_sets_the_next_wait_within_the_bounds() -> None:
    hint: list = [None]
    poller = _poller(lambda: 0, hint=lambda: hint[0], max_interval=120.0)
    hint[0] = 40.0
    assert poller.next_interval(2.0, False) == 40.0
    hint[0] = 0.0
    assert poller.next_interval(2.0, False) == 2.0  # never faster than the minimum
    hint[0] = 3600.0
    assert poller.next_interval(2.0, False) == 120.0  # never slower than the maximum
    hint[0] = None
    assert poller.next_interval(30.0, False) == 60.0  # unknown: back off exponentially
    assert poller.next_interval(30.0, True) == 2.0  # work always wins over the hint


def test_a_wake_by_name_reaches_the_registered_poller_and_stop_all_clears_it() -> None:
    poller = background.register(_poller(lambda: 0))
    background.wake("t")
    assert poller._wake.is_set()
    background.wake("nobody")  # an unknown name is a no-op
    background.stop_all()
    assert poller._stop.is_set() and background._pollers == {}


def test_a_running_worker_is_woken_early_and_stops_on_request() -> None:
    ticks: list[int] = []
    poller = AdaptivePoller("t", lambda: ticks.append(1), min_interval=0.05, max_interval=30.0, hint=lambda: 60.0)
    thread = threading.Thread(target=poller.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not ticks and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ticks, "the first tick runs immediately"

    before = len(ticks)  # the worker now sleeps for the 30 s maximum; a wake must cut that short
    poller.wake()
    deadline = time.monotonic() + 5
    while len(ticks) == before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(ticks) > before

    poller.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
