"""Common immutable market snapshot for multi-agent analysis.

All specialist agents must consume the same versioned snapshot. This module is
provider-neutral: it does not import brokers or vendor SDKs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from grow.market_data.provenance import MarketDataSource, classify_fixture_flags


SCHEMA = "grow.agent.market_snapshot.v1"


class DataQualityStatus(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    INSUFFICIENT = "INSUFFICIENT"
    REJECTED = "REJECTED"


def freeze_map(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_map(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze_map(item) for item in value)
    return value


def thaw_map(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_map(item) for key, item in value.items()}
    if isinstance(value, tuple) and not isinstance(value, (str, bytes)):
        return [thaw_map(item) for item in value]
    return value


@dataclass(frozen=True)
class UnderlyingQuoteView:
    underlying: str
    exchange: str
    spot: float | None
    ltp: float | None
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    quote_timestamp: datetime | None
    quote_age_seconds: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "exchange": self.exchange,
            "spot": self.spot,
            "ltp": self.ltp,
            "ohlc": {
                "open": self.open,
                "high": self.high,
                "low": self.low,
                "close": self.close,
            },
            "volume": self.volume,
            "quote_timestamp": None
            if self.quote_timestamp is None
            else self.quote_timestamp.isoformat(),
            "quote_age_seconds": self.quote_age_seconds,
        }


@dataclass(frozen=True)
class OptionQuoteView:
    underlying: str
    expiry: date
    strike: float
    option_type: str
    ltp: float | None
    bid: float | None
    ask: float | None
    open_interest: int | None
    volume: int | None
    quote_timestamp: datetime
    quote_age_seconds: float | None
    provider_contract_id: str
    quality: DataQualityStatus
    # Contract metadata preserved from the source when available.
    lot_size: int | None = None
    previous_open_interest: int | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    expiry_class: str | None = None
    # Per-quote market-data provenance. Mixing True and False → MIXED snapshot.
    is_fixture: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type,
            "ltp": self.ltp,
            "bid": self.bid,
            "ask": self.ask,
            "open_interest": self.open_interest,
            "volume": self.volume,
            "quote_timestamp": self.quote_timestamp.isoformat(),
            "quote_age_seconds": self.quote_age_seconds,
            "provider_contract_id": self.provider_contract_id,
            "quality": self.quality.value,
            "lot_size": self.lot_size,
            "previous_open_interest": self.previous_open_interest,
            "implied_volatility": self.implied_volatility,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
            "expiry_class": self.expiry_class,
            "is_fixture": self.is_fixture,
        }


@dataclass(frozen=True)
class AgentMarketSnapshot:
    """Point-in-time market state shared by every agent in a decision cycle."""

    snapshot_id: str
    version: str
    schema: str
    provider: str
    exchange: str
    session_timestamp: datetime
    decision_timestamp: datetime
    session_date: date
    underlyings: Mapping[str, UnderlyingQuoteView]
    option_contracts: tuple[OptionQuoteView, ...]
    data_quality: DataQualityStatus
    quality_notes: tuple[str, ...]
    source_snapshot_ids: Mapping[str, str]
    diagnostics: Mapping[str, Any]
    paper_mode: bool = True
    live_trading: bool = False
    # Fixture vs live classification of the *market data* source.
    # Paper execution is simulated; that does not make the quotes fixtures.
    # True only when market_data_source is FIXTURE (never when MIXED).
    is_fixture: bool = False
    market_data_source: MarketDataSource = MarketDataSource.LIVE

    def __post_init__(self) -> None:
        if self.live_trading:
            raise ValueError("AgentMarketSnapshot.live_trading must be false")
        if not self.paper_mode:
            raise ValueError("AgentMarketSnapshot.paper_mode must be true")
        source = self.market_data_source
        if not isinstance(source, MarketDataSource):
            source = MarketDataSource(str(source))
        if self.option_contracts:
            source = classify_fixture_flags(row.is_fixture for row in self.option_contracts)
        object.__setattr__(self, "market_data_source", source)
        # Fully fixture only when classification is FIXTURE — never when MIXED.
        object.__setattr__(self, "is_fixture", source is MarketDataSource.FIXTURE)
        object.__setattr__(self, "underlyings", MappingProxyType(dict(self.underlyings)))
        object.__setattr__(self, "source_snapshot_ids", freeze_map(dict(self.source_snapshot_ids)))
        object.__setattr__(self, "diagnostics", freeze_map(dict(self.diagnostics)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "version": self.version,
            "schema": self.schema,
            "provider": self.provider,
            "exchange": self.exchange,
            "session_timestamp": self.session_timestamp.isoformat(),
            "decision_timestamp": self.decision_timestamp.isoformat(),
            "session_date": self.session_date.isoformat(),
            "underlyings": {key: value.to_dict() for key, value in self.underlyings.items()},
            "option_contracts": [row.to_dict() for row in self.option_contracts],
            "data_quality": self.data_quality.value,
            "quality_notes": list(self.quality_notes),
            "source_snapshot_ids": thaw_map(self.source_snapshot_ids),
            "diagnostics": thaw_map(self.diagnostics),
            "paper_mode": True,
            "live_trading": False,
            "is_fixture": self.is_fixture,
            "market_data_source": self.market_data_source.value,
        }


def snapshot_digest(parts: Mapping[str, Any]) -> str:
    body = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
