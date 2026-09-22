"""NSE cash-market session calendar (milestone 1 approximation).

This is an interface with a deterministic implementation, not a live holiday
feed. Weekend + a small 2026 holiday set. Validate independently before
relying on it for anything beyond paper research.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Iterable

from grow.clock import IST, Clock, SystemClock
from grow.config import MarketConfig
from grow.types import SessionState

# NSE cash holidays 2026 — short seed list, not the official circular.
# Replace with an official calendar in a later data milestone.
CALENDAR_VERSION = "nse.session.cash.2026.v1"
_HOLIDAYS_2026 = frozenset(
    {
        date(2026, 1, 26),  # Republic Day
        date(2026, 3, 3),  # Holi
        date(2026, 3, 26),  # Ramzan Id (approx; confirm)
        date(2026, 4, 3),  # Good Friday
        date(2026, 4, 14),  # Dr. Ambedkar Jayanti
        date(2026, 8, 15),  # Independence Day
        date(2026, 10, 2),  # Gandhi Jayanti
        date(2026, 10, 20),  # Dussehra
        date(2026, 11, 8),  # Diwali (Laxmi Pujan) — confirm circular
        date(2026, 12, 25),  # Christmas
    }
)
CASH_HOLIDAYS_2026 = _HOLIDAYS_2026


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


class SessionCalendar:
    def __init__(
        self,
        market: MarketConfig,
        clock: Clock | None = None,
        holidays: Iterable[date] | None = None,
    ) -> None:
        self.market = market
        self.clock = clock or SystemClock()
        self.holidays = frozenset(holidays) if holidays is not None else _HOLIDAYS_2026
        self.open_time = _parse_hhmm(market.session_open)
        self.close_time = _parse_hhmm(market.session_close)
        self.square_off_time = _parse_hhmm(market.square_off)

    def state(self, at: datetime | None = None) -> SessionState:
        moment = (at or self.clock.now()).astimezone(IST)
        day = moment.date()
        if day.weekday() >= 5:
            return SessionState.WEEKEND
        if day in self.holidays:
            return SessionState.HOLIDAY
        now_t = moment.time().replace(microsecond=0)
        if now_t < self.open_time:
            return SessionState.PRE_OPEN
        if now_t >= self.close_time:
            return SessionState.CLOSED
        if now_t >= self.square_off_time:
            return SessionState.SQUARE_OFF_WINDOW
        return SessionState.OPEN

    def allows_new_entries(self, at: datetime | None = None) -> bool:
        return self.state(at) is SessionState.OPEN

    def requires_square_off(self, at: datetime | None = None) -> bool:
        return self.state(at) is SessionState.SQUARE_OFF_WINDOW
