"""Injectable clocks. Production uses IST. Tests freeze time."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def __init__(self, tz: ZoneInfo = IST) -> None:
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(self.tz)


class FrozenClock(Clock):
    def __init__(self, when: datetime) -> None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=IST)
        self._when = when.astimezone(IST)

    def now(self) -> datetime:
        return self._when

    def advance(self, delta: timedelta) -> datetime:
        """Move the frozen instant forward. Tests own scheduling; this is not a runner."""
        self._when = self._when + delta
        return self._when
