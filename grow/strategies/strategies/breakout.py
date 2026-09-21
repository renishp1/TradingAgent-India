"""Breakout / breakdown v1. Rolling high/low exclude the current bar."""

from __future__ import annotations

from grow.data.schema import MarketSnapshot
from grow.strategies.emit import atr_levels, research_signal
from grow.strategies.models import Direction, MarketRegime, StrategyContext
from grow.strategies.signal import StrategySignal


class BreakoutStrategy:
    name = "breakout"
    version = "v1"

    def evaluate(self, snapshot: MarketSnapshot, context: StrategyContext) -> StrategySignal | None:
        ind = context.indicators
        if not ind.ready("rolling_high", "rolling_low", "atr"):
            return None
        assert ind.rolling_high is not None and ind.rolling_low is not None and ind.atr is not None
        lookback = context.params.get("lookback", 20)
        regime = context.regime.label
        bull = ind.close > ind.rolling_high and (ind.body or 0) > 0
        bear = ind.close < ind.rolling_low and (ind.body or 0) < 0
        if bull and regime not in {MarketRegime.RANGE, MarketRegime.BEAR_TREND}:
            stop, target = atr_levels(ind.close, ind.atr, Direction.BULLISH)
            stop = min(stop, ind.rolling_low)
            return research_signal(
                snapshot,
                context,
                name=self.name,
                direction=Direction.BULLISH,
                entry=ind.close,
                stop=stop,
                target=target,
                confidence=0.55,
                reason=(
                    f"close={ind.close:.2f} > prior {lookback}-bar high {ind.rolling_high:.2f} "
                    f"(current bar excluded), regime={regime.value}"
                ),
            )
        if bear and regime not in {MarketRegime.RANGE, MarketRegime.BULL_TREND}:
            stop, target = atr_levels(ind.close, ind.atr, Direction.BEARISH)
            stop = max(stop, ind.rolling_high)
            return research_signal(
                snapshot,
                context,
                name=self.name,
                direction=Direction.BEARISH,
                entry=ind.close,
                stop=stop,
                target=target,
                confidence=0.55,
                reason=(
                    f"close={ind.close:.2f} < prior {lookback}-bar low {ind.rolling_low:.2f} "
                    f"(current bar excluded), regime={regime.value}"
                ),
            )
        return None
