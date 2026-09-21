"""Shared value objects for Grow.

These are plain dataclasses on purpose: milestone 1 has no pydantic / LangGraph
dependency. Serialization is JSON-friendly via `asdict`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Intent(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    REDUCE = "REDUCE"
    SQUARE_OFF = "SQUARE_OFF"


class Venue(str, Enum):
    PAPER = "PAPER"
    # LIVE is intentionally absent from this enum.


class SessionState(str, Enum):
    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    SQUARE_OFF_WINDOW = "SQUARE_OFF_WINDOW"
    CLOSED = "CLOSED"
    HOLIDAY = "HOLIDAY"
    WEEKEND = "WEEKEND"


class Regime(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Symbol:
    ticker: str
    exchange: str = "NSE"

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", self.ticker.strip().upper())
        object.__setattr__(self, "exchange", self.exchange.strip().upper())

    def qualified(self) -> str:
        return f"{self.ticker}.{self.exchange}"


@dataclass(frozen=True)
class MarketBrief:
    symbol: Symbol
    as_of: datetime
    session: SessionState
    last_price: float
    currency: str
    regime: Regime
    notes: tuple[str, ...] = ()
    source: str = "grow.market.stub"
    extras: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["symbol"] = {"ticker": self.symbol.ticker, "exchange": self.symbol.exchange}
        payload["session"] = self.session.value
        payload["regime"] = self.regime.value
        payload["as_of"] = self.as_of.isoformat()
        return payload


@dataclass(frozen=True)
class TradeProposal:
    """CEO output. Not executable until Risk Guard stamps it."""

    proposal_id: str
    symbol: Symbol
    side: Side
    intent: Intent
    quantity: int
    limit_price: float
    stop_loss: float | None
    take_profit: float | None
    thesis: str
    confidence: float
    venue: Venue
    created_at: datetime
    notional: float
    extras: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["symbol"] = {"ticker": self.symbol.ticker, "exchange": self.symbol.exchange}
        payload["side"] = self.side.value
        payload["intent"] = self.intent.value
        payload["venue"] = self.venue.value
        payload["created_at"] = self.created_at.isoformat()
        return payload


@dataclass(frozen=True)
class RiskStamp:
    """Capability token minted only by Risk Guard.

    Paper ledger refuses any proposal whose stamp does not verify.
    """

    proposal_id: str
    issued_at: datetime
    ruleset: str
    token: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "issued_at": self.issued_at.isoformat(),
            "ruleset": self.ruleset,
            "token": self.token,
        }


@dataclass(frozen=True)
class RiskVerdict:
    approved: bool
    rule_results: tuple[tuple[str, bool, str], ...]
    stamp: RiskStamp | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "stamp": None if self.stamp is None else self.stamp.to_dict(),
            "rule_results": [
                {"id": rid, "passed": passed, "detail": detail}
                for rid, passed, detail in self.rule_results
            ],
        }


@dataclass(frozen=True)
class Fill:
    fill_id: str
    proposal_id: str
    symbol: Symbol
    side: Side
    quantity: int
    price: float
    notional: float
    filled_at: datetime
    venue_id: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["symbol"] = {"ticker": self.symbol.ticker, "exchange": self.symbol.exchange}
        payload["side"] = self.side.value
        payload["filled_at"] = self.filled_at.isoformat()
        return payload
