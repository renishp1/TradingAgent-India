"""Historical session calendar from a 2G dataset. Not weekday inference."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from grow.backtest.calendar import ExplicitSessionCalendar
from grow.clock import IST
from grow.history.store import CanonicalStore
from grow.types import SessionState


def calendar_for(store: CanonicalStore, start: date, end: date) -> ExplicitSessionCalendar:
    days = tuple(session.session_date for session in store.sessions(start, end))
    return ExplicitSessionCalendar(days, version=store.meta.calendar_version)


def session_state_at(store: CanonicalStore, as_of: datetime) -> SessionState:
    moment = as_of.astimezone(IST)
    day = moment.date()
    session = store.session_on(day)
    if session is None:
        return SessionState.WEEKEND if day.weekday() >= 5 else SessionState.HOLIDAY
    if session.status == "CLOSED":
        reason = (session.special_reason or "").lower()
        return SessionState.HOLIDAY if reason == "holiday" else SessionState.CLOSED
    if moment < session.open_at:
        return SessionState.PRE_OPEN
    if moment >= session.close_at:
        return SessionState.CLOSED
    if moment >= session.close_at - timedelta(minutes=15):
        return SessionState.SQUARE_OFF_WINDOW
    return SessionState.OPEN
