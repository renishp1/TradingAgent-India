"""EMA trend v1. BULLISH and BEARISH research. Not an order."""

from __future__ import annotations

from grow.data.schema import MarketSnapshot
from grow.strategies.confidence import format_parts, regime_alignment, score_confidence, unit
from grow.strategies.emit import atr_levels, research_signal
from grow.strategies.models import Direction, MarketRegime, StrategyContext
from grow.strategies.signal import StrategySignal


class EmaTrendStrategy:
    name = "ema_trend"
    version = "v1"

    def evaluate(self, snapshot: MarketSnapshot, context: StrategyContext) -> StrategySignal | None:
        ind = context.indicators
        if not ind.ready("ema_fast", "ema_slow", "ema_fast_slope", "atr"):
            return None
        assert ind.ema_fast is not None and ind.ema_slow is not None
        assert ind.ema_fast_slope is not None and ind.atr is not None
        fast_p = context.params.get("fast_period", 20)
        slow_p = context.params.get("slow_period", 50)
        regime = context.regime.label
        bull = ind.ema_fast > ind.ema_slow and ind.ema_fast_slope > 0 and ind.close > ind.ema_fast
        bear = ind.ema_fast < ind.ema_slow and ind.ema_fast_slope < 0 and ind.close < ind.ema_fast
        if bull and regime not in {MarketRegime.BEAR_TREND, MarketRegime.RANGE}:
            return self._emit(snapshot, context, Direction.BULLISH, fast_p, slow_p)
        if bear and regime not in {MarketRegime.BULL_TREND, MarketRegime.RANGE}:
            return self._emit(snapshot, context, Direction.BEARISH, fast_p, slow_p)
        return None

    def _emit(self, snapshot, context, direction: Direction, fast_p, slow_p) -> StrategySignal | None:
        ind = context.indicators
        assert ind.ema_fast is not None and ind.ema_slow is not None
        assert ind.ema_fast_slope is not None and ind.atr is not None
        stop, target = atr_levels(ind.close, ind.atr, direction)
        if direction is Direction.BULLISH:
            stop = min(stop, ind.ema_fast) if stop < ind.close else stop
            stack = (
                f"EMA{fast_p}={ind.ema_fast:.2f} > EMA{slow_p}={ind.ema_slow:.2f}, "
                f"price={ind.close:.2f} above fast EMA, slope={ind.ema_fast_slope:.4f}"
            )
        else:
            stop = max(stop, ind.ema_fast) if stop > ind.close else stop
            stack = (
                f"EMA{fast_p}={ind.ema_fast:.2f} < EMA{slow_p}={ind.ema_slow:.2f}, "
                f"price={ind.close:.2f} below fast EMA, slope={ind.ema_fast_slope:.4f}"
            )
        parts = {
            "trend_alignment": unit(ind.ema_fast - ind.ema_slow, 0.015 * ind.close),
            "indicator_strength": unit(ind.ema_fast_slope, 0.0005 * ind.close),
            "structure_confirmation": unit(ind.close - ind.ema_fast, 0.01 * ind.close),
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
