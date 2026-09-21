"""Market-data value objects (Milestone 2A).

These types are the only legal shape for OHLCV inside Grow. They are not
trades. They are not broker messages. A later licensed adapter must emit
these objects; it must not leak vendor JSON into the CEO or the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from grow.types import SessionState, Symbol


class Timeframe(str, Enum):
    D1 = "D1"
    M15 = "M15"
    M5 = "M5"


class InstrumentKind(str, Enum):
    EQUITY = "EQUITY"
    INDEX = "INDEX"


@dataclass(frozen=True)
class SourceMeta:
    """Who produced the bars, and under what claim."""

    name: str
    vendor: str
    license: str
    is_live: bool
    is_fixture: bool
    schema: str = "grow.data.snapshot.v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "vendor": self.vendor,
            "license": self.license,
            "is_live": self.is_live,
            "is_fixture": self.is_fixture,
            "schema": self.schema,
        }


FIXTURE_SOURCE = SourceMeta(
    name="grow.data.fixture.v1",
    vendor="grow",
    license="synthetic-not-licensed",
    is_live=False,
    is_fixture=True,
)


@dataclass(frozen=True)
class Bar:
    symbol: Symbol
    timeframe: Timeframe
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("bar end must be after start")
        if self.volume < 0:
            raise ValueError("volume must be >= 0")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC envelope is inconsistent")
        if self.high < self.low:
            raise ValueError("high < low")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol.qualified(),
            "timeframe": self.timeframe.value,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class BarSeries:
    symbol: Symbol
    timeframe: Timeframe
    bars: tuple[Bar, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol.qualified(),
            "timeframe": self.timeframe.value,
            "count": len(self.bars),
            "bars": [bar.to_dict() for bar in self.bars],
        }


@dataclass(frozen=True)
class SnapshotQuality:
    complete: bool
    stale: bool
    missing_count: int
    expected_count: int
    last_bar_end: datetime | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "stale": self.stale,
            "missing_count": self.missing_count,
            "expected_count": self.expected_count,
            "last_bar_end": None if self.last_bar_end is None else self.last_bar_end.isoformat(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class MarketSnapshot:
    """Normalized view of one symbol at one clock.

    `series` is for quantitative code only. LLM research must consume
    `ResearchView`, never this object.
    """

    snapshot_id: str
    symbol: Symbol
    as_of: datetime
    session: SessionState
    last_price: float
    currency: str
    series: Mapping[Timeframe, BarSeries]
    quality: SnapshotQuality
    source: SourceMeta

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol.qualified(),
            "as_of": self.as_of.isoformat(),
            "session": self.session.value,
            "last_price": self.last_price,
            "currency": self.currency,
            "quality": self.quality.to_dict(),
            "source": self.source.to_dict(),
            "bar_counts": {tf.value: len(series.bars) for tf, series in self.series.items()},
        }


@dataclass(frozen=True)
class ResearchView:
    """The only market-data shape an LLM is allowed to see in 2A/2B.

    No OHLCV arrays. Quant owns the candles; research owns the summary.
    """

    snapshot_id: str
    symbol: Symbol
    as_of: datetime
    session: SessionState
    last_price: float
    currency: str
    source_name: str
    is_fixture: bool
    quality_complete: bool
    quality_stale: bool
    missing_count: int
    bar_counts: Mapping[str, int]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol.qualified(),
            "as_of": self.as_of.isoformat(),
            "session": self.session.value,
            "last_price": self.last_price,
            "currency": self.currency,
            "source_name": self.source_name,
            "is_fixture": self.is_fixture,
            "quality_complete": self.quality_complete,
            "quality_stale": self.quality_stale,
            "missing_count": self.missing_count,
            "bar_counts": dict(self.bar_counts),
            "notes": list(self.notes),
        }
