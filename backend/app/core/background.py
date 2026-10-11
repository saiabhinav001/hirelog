"""Idle-adaptive polling for the exam workers: a fixed 2 s poll costs ~43k Firestore reads a day per loop, which alone
exceeds the free (Spark) daily read quota of 50k."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("hirelog.exams.background")


class AdaptivePoller:
    """Runs `tick` at `min_interval` while it finds work, backs off to `max_interval` while idle, and is woken at once
    by in-process work (`wake`). `hint` may name how long until the next tick is actually needed (None = unknown)."""

    def __init__(
        self,
        name: str,
        tick: Callable[[], object],
        *,
        min_interval: float,
        max_interval: float,
        burst_seconds: float = 30.0,
        hint: Optional[Callable[[], Optional[float]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._tick = tick
        self._min = max(0.05, float(min_interval))
        self._max = max(self._min, float(max_interval))
        self._burst = burst_seconds
        self._hint = hint
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._fast_until = 0.0
        self.ticks = 0

    def wake(self) -> None:
        # Stay fast for a while: the commit that triggered the wake may land just after it.
        self._fast_until = self._clock() + self._burst
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def next_interval(self, current: float, did_work: bool) -> float:
        if did_work or self._clock() < self._fast_until:
            return self._min
        wait = self._hint() if self._hint is not None else None
        if wait is not None:
            return min(max(wait, self._min), self._max)
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
            self._wake.wait(timeout=interval)
            self._wake.clear()


_pollers: dict[str, AdaptivePoller] = {}


def register(poller: AdaptivePoller) -> AdaptivePoller:
    _pollers[poller.name] = poller
    return poller


def wake(name: str) -> None:
    poller = _pollers.get(name)
    if poller is not None:
        poller.wake()


def stop_all() -> None:
    for poller in list(_pollers.values()):
        poller.stop()
    _pollers.clear()
