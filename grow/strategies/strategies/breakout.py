"""Breakout / breakdown v1. Rolling high/low exclude the current bar."""

from __future__ import annotations

from grow.data.schema import MarketSnapshot
from grow.strategies.confidence import format_parts, regime_alignment, score_confidence, unit
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
            return self._emit(snapshot, context, Direction.BULLISH, lookback)
        if bear and regime not in {MarketRegime.RANGE, MarketRegime.BULL_TREND}:
            return self._emit(snapshot, context, Direction.BEARISH, lookback)
        return None

    def _emit(self, snapshot, context, direction: Direction, lookback) -> StrategySignal | None:
        ind = context.indicators
        assert ind.rolling_high is not None and ind.rolling_low is not None and ind.atr is not None
        stop, target = atr_levels(ind.close, ind.atr, direction)
        if direction is Direction.BULLISH:
            stop = min(stop, ind.rolling_low)
            level = ind.rolling_high
            stack = (
                f"close={ind.close:.2f} > prior {lookback}-bar high {ind.rolling_high:.2f} "
                f"(current bar excluded)"
            )
        else:
            stop = max(stop, ind.rolling_high)
            level = ind.rolling_low
            stack = (
                f"close={ind.close:.2f} < prior {lookback}-bar low {ind.rolling_low:.2f} "
                f"(current bar excluded)"
            )
        body = abs(ind.body or 0.0)
        parts = {
            "trend_alignment": unit(body, ind.atr),
            "indicator_strength": unit(ind.close - level, ind.atr),
            "structure_confirmation": unit(body, ind.range or ind.atr),
            "regime_alignment": regime_alignment(direction, context.regime.label, style="trend"),
        }
        confidence = score_confidence(parts)
        return research_signal(
            snapshot,
            context,
            name=self.name,
            direction=direction,
            entry=ind.close,
            stop=stop,
            target=target,
            confidence=confidence,
            reason=f"{stack}, regime={context.regime.label.value}, {format_parts(parts, confidence)}",
            extras={"confidence_parts": parts},
        )
