"""Versioned session calendars.

`WeekdayFixtureCalendar` (`nse.weekday.v1`) is for fixture/unit tests only.
It is not an NSE holiday calendar. Historical validation must pass an
`ExplicitSessionCalendar` built from an exchange session list.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Protocol

from grow.clock import IST
from grow.errors import GrowConfigError

SQUARE_OFF = time(15, 15)
DECISION_TIME = time(11, 0)
FIXTURE_CALENDAR = "nse.weekday.v1"
EXPLICIT_CALENDAR = "nse.session.explicit.v1"


class SessionCalendar(Protocol):
    version: str

    def sessions(self, start: date, end: date) -> tuple[date, ...]: ...


class WeekdayFixtureCalendar:
    """Weekdays as sessions. Fixture/unit tests only — not NSE holidays."""

    version = FIXTURE_CALENDAR

    def sessions(self, start: date, end: date) -> tuple[date, ...]:
        days: list[date] = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor += timedelta(days=1)
        return tuple(days)


class ExplicitSessionCalendar:
    """Exchange-provided session dates. Required for non-fixture historical runs."""

    def __init__(self, days: tuple[date, ...], *, version: str = EXPLICIT_CALENDAR) -> None:
        if not days:
            raise GrowConfigError("explicit session calendar is empty")
        self.version = version
        self._days = tuple(sorted(set(days)))

    def sessions(self, start: date, end: date) -> tuple[date, ...]:
        return tuple(day for day in self._days if start <= day <= end)


def weekday_sessions(start: date, end: date) -> tuple[date, ...]:
    """Fixture helper. Do not use as a production NSE calendar."""
    return WeekdayFixtureCalendar().sessions(start, end)


def at_session(day: date, clock: time) -> datetime:
    return datetime(day.year, day.month, day.day, clock.hour, clock.minute, tzinfo=IST)


CALENDAR_VERSION = FIXTURE_CALENDAR
