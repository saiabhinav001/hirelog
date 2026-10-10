"""Adaptive background polling for the free-tier (Spark) Firestore budget.

A fixed 2-second poll costs ~43k Firestore reads/day per loop even when
nothing is happening, which on its own exceeds the whole Spark daily read
quota. These loops instead back off exponentially while idle and are woken
immediately by in-process work (`wake(name)`), then stay on the fast cadence
for a short burst so a commit that lands just after the wake is still seen.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("hirelog.exams.background")


class AdaptivePoller:
    def __init__(
        self,
        name: str,
        tick: Callable[[], object],
        *,
        min_interval: float,
        max_interval: float,
        burst_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._tick = tick
        self._min = max(0.05, float(min_interval))
        self._max = max(self._min, float(max_interval))
        self._burst = burst_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._fast_until = 0.0
        self.ticks = 0
        self.last_interval = self._min

    def wake(self) -> None:
        self._fast_until = self._clock() + self._burst
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def next_interval(self, current: float, did_work: bool) -> float:
        if did_work or self._clock() < self._fast_until:
            return self._min
        return min(current * 2.0, self._max)

    def run_once(self, current: float) -> float:
        did_work = False
        try:
            did_work = bool(self._tick())
        except Exception:  # noqa: BLE001 - a failing tick must never kill the loop
            logger.exception("%s tick failed", self.name)
        self.ticks += 1
        return self.next_interval(current, did_work)

    def run(self) -> None:
        interval = self._min
        while not self._stop.is_set():
            interval = self.run_once(interval)
            self.last_interval = interval
            self._wake.wait(timeout=interval)
            self._wake.clear()


_pollers: dict[str, AdaptivePoller] = {}


def register(poller: AdaptivePoller) -> AdaptivePoller:
    _pollers[poller.name] = poller
    return poller


def wake(name: str) -> None:
    poller: Optional[AdaptivePoller] = _pollers.get(name)
    if poller is not None:
        poller.wake()


def stop_all() -> None:
    for poller in list(_pollers.values()):
        poller.stop()
    _pollers.clear()


def start_daemon(poller: AdaptivePoller) -> threading.Thread:
    register(poller)
    thread = threading.Thread(target=poller.run, name=f"exam-{poller.name}", daemon=True)
    thread.start()
    return thread
