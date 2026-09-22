"""Historical session calendar from a 2G dataset. Not weekday inference."""

from __future__ import annotations

from datetime import date

from grow.backtest.calendar import ExplicitSessionCalendar
from grow.history.store import CanonicalStore


def calendar_for(store: CanonicalStore, start: date, end: date) -> ExplicitSessionCalendar:
    days = tuple(session.session_date for session in store.sessions(start, end))
    return ExplicitSessionCalendar(days, version=store.meta.calendar_version)
