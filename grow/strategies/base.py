"""Strategy protocol. Pure research function — no IO, no mutation."""

from __future__ import annotations

from typing import Protocol

from grow.data.schema import MarketSnapshot
from grow.strategies.models import StrategyContext
from grow.strategies.signal import StrategySignal


class Strategy(Protocol):
    name: str
    version: str

    def evaluate(self, snapshot: MarketSnapshot, context: StrategyContext) -> StrategySignal | None:
        """Return a BULLISH or BEARISH research signal, or None. Never an order."""
        ...
