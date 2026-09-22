"""Deterministic paper exits. No AI. No trailing stop."""

from __future__ import annotations

from dataclasses import dataclass

from grow.market.session import SessionCalendar
from grow.types import SessionState


class ExitReason:
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    SESSION_CLOSE = "SESSION_CLOSE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


@dataclass(frozen=True)
class ExitDecision:
    position_id: str
    reason: str
    mark_price: float
    price_source: str


def session_close_due(calendar: SessionCalendar, as_of, *, enabled: bool) -> bool:
    if not enabled:
        return False
    state = calendar.state(as_of)
    return state in {SessionState.SQUARE_OFF_WINDOW, SessionState.CLOSED}


def choose_exit(
    *,
    mark_price: float,
    stop_loss_price: float,
    take_profit_price: float,
    session_close: bool,
) -> str | None:
    """Priority: STOP_LOSS, TAKE_PROFIT, SESSION_CLOSE."""
    if mark_price <= stop_loss_price:
        return ExitReason.STOP_LOSS
    if mark_price >= take_profit_price:
        return ExitReason.TAKE_PROFIT
    if session_close:
        return ExitReason.SESSION_CLOSE
    return None
