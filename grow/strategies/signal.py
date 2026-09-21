"""StrategySignal — Milestone 2B contract, not produced in 2A.

Quant calculations over MarketSnapshot emit this object. The LLM debates
the signal; it does not invent one from raw candles.
"""

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
    extras: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.direction != "LONG":
            # Cash book is long-only; 2B must not emit SHORT.
            raise ValueError("2A/2B cash signals are LONG only")
        return {
            "symbol": self.symbol.qualified(),
            "strategy": self.strategy,
            "direction": self.direction,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "confidence": self.confidence,
            "timeframe": self.timeframe.value,
            "reason": self.reason,
            "as_of": self.as_of.isoformat(),
            "snapshot_id": self.snapshot_id,
        }
