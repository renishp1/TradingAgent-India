"""Historical data quality rules. Fail closed. Do not repair OHLC."""

from __future__ import annotations

from datetime import datetime

from grow.errors import GrowConfigError
from grow.history.models import (
    ALLOWED_OPTION_TYPES,
    HistoricalBar,
    HistoricalOptionContract,
    HistoricalOptionQuote,
    HistoricalSession,
)
from grow.history.universe import is_forbidden_instrument


def require_aware(ts: datetime, label: str) -> None:
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise GrowConfigError(f"NAIVE_TIMESTAMP:{label}")


def validate_bar(bar: HistoricalBar) -> None:
    require_aware(bar.timestamp, "bar.timestamp")
    require_aware(bar.end, "bar.end")
    require_aware(bar.as_of_available_at, "bar.available")
    if is_forbidden_instrument(bar.symbol):
        raise GrowConfigError(f"UNSUPPORTED_UNDERLYING:{bar.symbol}")
    if bar.volume < 0:
        raise GrowConfigError("NEGATIVE_VOLUME")
    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
        raise GrowConfigError("INVALID_OHLC")
    if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close) or bar.high < bar.low:
        raise GrowConfigError("INVALID_OHLC")
    if bar.timestamp > bar.as_of_available_at:
        raise GrowConfigError("FUTURE_TIMESTAMP")
    if bar.end < bar.timestamp:
        raise GrowConfigError("INVALID_OHLC")


def validate_contract(contract: HistoricalOptionContract) -> None:
    require_aware(contract.first_seen_at, "contract.first_seen")
    require_aware(contract.last_seen_at, "contract.last_seen")
    if is_forbidden_instrument(contract.underlying):
        raise GrowConfigError(f"UNSUPPORTED_UNDERLYING:{contract.underlying}")
    if contract.option_type not in ALLOWED_OPTION_TYPES:
        raise GrowConfigError(f"UNSUPPORTED_OPTION_TYPE:{contract.option_type}")
    if contract.strike <= 0:
        raise GrowConfigError("INVALID_STRIKE")
    if contract.lot_size is not None and contract.lot_size < 1:
        raise GrowConfigError("INVALID_LOT_SIZE")
    if contract.first_seen_at > contract.last_seen_at:
        raise GrowConfigError("CONTRACT_WINDOW")


def validate_quote(quote: HistoricalOptionQuote) -> None:
    require_aware(quote.timestamp, "quote.timestamp")
    require_aware(quote.as_of_available_at, "quote.available")
    if quote.timestamp > quote.as_of_available_at:
        raise GrowConfigError("FUTURE_TIMESTAMP")
    if quote.bid is not None and quote.bid < 0:
        raise GrowConfigError("NEGATIVE_BID")
    if quote.ask is not None and quote.ask < 0:
        raise GrowConfigError("NEGATIVE_ASK")
    if quote.bid is not None and quote.ask is not None and quote.bid > quote.ask:
        raise GrowConfigError("CROSSED_QUOTE")
    if quote.volume is not None and quote.volume < 0:
        raise GrowConfigError("NEGATIVE_VOLUME")
    if quote.open_interest is not None and quote.open_interest < 0:
        raise GrowConfigError("NEGATIVE_OI")


def validate_session(session: HistoricalSession) -> None:
    require_aware(session.open_at, "session.open")
    require_aware(session.close_at, "session.close")
    if session.status not in {"OPEN", "CLOSED"}:
        raise GrowConfigError(f"SESSION_STATUS:{session.status}")
