"""Server-authoritative time source. Clients never set deadlines/epochs — every
domain function that needs "now" takes a `Clock`, never `datetime.now()`
directly, so tests can control time deterministically (contract: "Use a
controllable server clock in tests; clients never set authoritative time.")."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Production clock — real wall-clock UTC time."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """Test clock with an explicit, advanceable time. Never used in production."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def set(self, value: datetime) -> None:
        self._now = value


system_clock = SystemClock()
