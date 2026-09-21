"""Expected NSE cash bar starts.

Cash session 09:15–15:30 IST. Intraday bars tile that window:

- M15: 25 bars, last start 15:15
- M5: 75 bars, last start 15:25
- D1: one bar covering the session
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from grow.clock import IST
from grow.data.schema import Timeframe
from grow.market.session import SessionCalendar


def bar_duration(timeframe: Timeframe) -> timedelta:
    if timeframe is Timeframe.D1:
        return timedelta(hours=6, minutes=15)
    if timeframe is Timeframe.M15:
        return timedelta(minutes=15)
    if timeframe is Timeframe.M5:
        return timedelta(minutes=5)
    raise ValueError(f"unsupported timeframe {timeframe}")


def expected_starts(
    session_day: date,
    timeframe: Timeframe,
    *,
    session_open: time,
    session_close: time,
) -> tuple[datetime, ...]:
    open_at = datetime.combine(session_day, session_open, tzinfo=IST)
    close_at = datetime.combine(session_day, session_close, tzinfo=IST)
    if timeframe is Timeframe.D1:
        return (open_at,)
    duration = bar_duration(timeframe)
    starts: list[datetime] = []
    cursor = open_at
    while cursor + duration <= close_at:
        starts.append(cursor)
        cursor += duration
    return tuple(starts)


def complete_starts(
    session_day: date,
    timeframe: Timeframe,
    as_of: datetime,
    *,
    session_open: time,
    session_close: time,
) -> tuple[datetime, ...]:
    as_of = as_of.astimezone(IST)
    duration = bar_duration(timeframe)
    return tuple(
        start
        for start in expected_starts(
            session_day, timeframe, session_open=session_open, session_close=session_close
        )
        if start + duration <= as_of
    )


def session_day_for(calendar: SessionCalendar, as_of: datetime) -> date | None:
    """Session whose bars are complete or in progress at `as_of`.

    Weekend / holiday / pre-open maps to the previous cash session.
    Returns None if no session exists in the lookback.
    """
    moment = as_of.astimezone(IST)
    day = moment.date()
    state = calendar.state(moment)
    if state.value in {"OPEN", "SQUARE_OFF_WINDOW", "CLOSED"}:
        return day
    # PRE_OPEN, WEEKEND, HOLIDAY → previous session day
    probe = day
    for _ in range(14):
        probe = probe - timedelta(days=1)
        probe_noon = datetime.combine(probe, time(12, 0), tzinfo=IST)
        if calendar.state(probe_noon).value in {"OPEN", "SQUARE_OFF_WINDOW", "CLOSED"}:
            return probe
    return None
