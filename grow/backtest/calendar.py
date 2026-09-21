"""Versioned weekday session list. Not a live holiday feed."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from grow.clock import IST

CALENDAR_VERSION = "nse.weekday.v1"
SQUARE_OFF = time(15, 15)
DECISION_TIME = time(11, 0)


def weekday_sessions(start: date, end: date) -> tuple[date, ...]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return tuple(days)


def at_session(day: date, clock: time) -> datetime:
    return datetime(day.year, day.month, day.day, clock.hour, clock.minute, tzinfo=IST)
