"""Immutable 2B contracts. Signals are research, not orders."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from grow.data.schema import Timeframe
from grow.types import SessionState, Symbol


class MarketRegime(str, Enum):
    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNKNOWN = "UNKNOWN"


class Direction(str, Enum):
    """Research direction. Not an execution side. Not SHORT/SELL."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


@dataclass(frozen=True)
class IndicatorSnapshot:
    timeframe: Timeframe
    close: float
    sma_fast: float | None
    ema_fast: float | None
    ema_slow: float | None
    ema_fast_slope: float | None
    rsi: float | None
    roc: float | None
    atr: float | None
    rolling_std: float | None
    prev_high: float | None
    prev_low: float | None
    rolling_high: float | None
    rolling_low: float | None
    range: float | None
    body: float | None
    upper_wick: float | None
    lower_wick: float | None
    bar_count: int

    def ready(self, *names: str) -> bool:
        for name in names:
            if getattr(self, name) is None:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "timeframe": self.timeframe.value,
            "close": self.close,
            "sma_fast": self.sma_fast,
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "ema_fast_slope": self.ema_fast_slope,
            "rsi": self.rsi,
            "roc": self.roc,
            "atr": self.atr,
            "rolling_std": self.rolling_std,
            "prev_high": self.prev_high,
            "prev_low": self.prev_low,
            "rolling_high": self.rolling_high,
            "rolling_low": self.rolling_low,
            "range": self.range,
            "body": self.body,
            "upper_wick": self.upper_wick,
            "lower_wick": self.lower_wick,
            "bar_count": self.bar_count,
        }
        return payload


@dataclass(frozen=True)
class RegimeSnapshot:
    label: MarketRegime
    d1_trend: str
    m15_trend: str
    volatility: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label.value,
            "d1_trend": self.d1_trend,
            "m15_trend": self.m15_trend,
            "volatility": self.volatility,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class StrategyContext:
    as_of: datetime
    timeframe: Timeframe
    regime: RegimeSnapshot
    indicators: IndicatorSnapshot
    snapshot_id: str
    session: SessionState
    params: Mapping[str, Any]


@dataclass(frozen=True)
class StrategySkip:
    strategy: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"strategy": self.strategy, "reason": self.reason}


@dataclass(frozen=True)
class StrategyResult:
    snapshot_id: str
    as_of: datetime
    symbol: Symbol
    regime: RegimeSnapshot
    signals: tuple
    evaluated: tuple[str, ...]
    skipped: tuple[StrategySkip, ...]
    diagnostics: tuple[str, ...]
    indicators: IndicatorSnapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.isoformat(),
            "symbol": self.symbol.qualified(),
            "regime": self.regime.to_dict(),
            "signals": [s.to_dict() for s in self.signals],
            "evaluated": list(self.evaluated),
            "skipped": [s.to_dict() for s in self.skipped],
            "diagnostics": list(self.diagnostics),
            "indicators": self.indicators.to_dict(),
        }


def signal_fingerprint(
    *,
    symbol: str,
    strategy: str,
    timeframe: str,
    as_of: str,
    snapshot_id: str,
    strategy_version: str,
    direction: str,
) -> str:
    body = json.dumps(
        {
            "as_of": as_of,
            "direction": direction,
            "snapshot_id": snapshot_id,
            "strategy": strategy,
            "symbol": symbol,
            "timeframe": timeframe,
            "version": strategy_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
