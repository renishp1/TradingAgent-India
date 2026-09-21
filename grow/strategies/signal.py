"""StrategySignal — Milestone 2B output. Not an order. Not an option."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from grow.data.schema import Timeframe
from grow.types import Symbol


@dataclass(frozen=True)
class StrategySignal:
    symbol: Symbol
    strategy: str
    direction: str
    entry: float
    stop: float
    target: float
    confidence: float
    timeframe: Timeframe
    reason: str
    as_of: datetime
    snapshot_id: str
    signal_id: str = ""
    strategy_version: str = "v1"
    regime: str = "UNKNOWN"
    risk_reward: float | None = None
    extras: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.direction not in {"BULLISH", "BEARISH"}:
            raise ValueError(
                "2B research direction is BULLISH or BEARISH. "
                "Not BUY/SELL/LONG/SHORT execution."
            )
        return {
            "symbol": self.symbol.qualified(),
            "strategy": self.strategy,
            "strategy_version": self.strategy_version,
            "direction": self.direction,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "confidence": self.confidence,
            "risk_reward": self.risk_reward,
            "timeframe": self.timeframe.value,
            "reason": self.reason,
            "as_of": self.as_of.isoformat(),
            "snapshot_id": self.snapshot_id,
            "signal_id": self.signal_id,
            "regime": self.regime,
        }
