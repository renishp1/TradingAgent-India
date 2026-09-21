"""Momentum v1. BULLISH and BEARISH research. Not an order."""

from __future__ import annotations

from grow.data.schema import MarketSnapshot
from grow.strategies.confidence import format_parts, regime_alignment, score_confidence, unit
from grow.strategies.emit import atr_levels, research_signal
from grow.strategies.models import Direction, MarketRegime, StrategyContext
from grow.strategies.signal import StrategySignal


class MomentumStrategy:
    name = "momentum"
    version = "v1"

    def evaluate(self, snapshot: MarketSnapshot, context: StrategyContext) -> StrategySignal | None:
        ind = context.indicators
        if not ind.ready("rsi", "roc", "ema_fast", "atr"):
            return None
        assert ind.rsi is not None and ind.roc is not None
        assert ind.ema_fast is not None and ind.atr is not None
        threshold = float(context.params.get("rsi_threshold", 55))
        bearish_rsi = 100.0 - threshold
        regime = context.regime.label
        bull = ind.rsi > threshold and ind.roc > 0 and ind.close > ind.ema_fast
        bear = ind.rsi < bearish_rsi and ind.roc < 0 and ind.close < ind.ema_fast
        if bull and regime is not MarketRegime.BEAR_TREND:
            return self._emit(snapshot, context, Direction.BULLISH, threshold, bearish_rsi)
        if bear and regime is not MarketRegime.BULL_TREND:
            return self._emit(snapshot, context, Direction.BEARISH, threshold, bearish_rsi)
        return None

    def _emit(self, snapshot, context, direction: Direction, threshold: float, bearish_rsi: float) -> StrategySignal | None:
        ind = context.indicators
        assert ind.rsi is not None and ind.roc is not None
        assert ind.ema_fast is not None and ind.atr is not None
        stop, target = atr_levels(ind.close, ind.atr, direction)
        parts = {
            "trend_alignment": unit(ind.close - ind.ema_fast, 0.01 * ind.close),
            "indicator_strength": unit(ind.rsi - 50.0, 30.0),
            "structure_confirmation": unit(ind.roc, 2.0),
            "regime_alignment": regime_alignment(direction, context.regime.label, style="trend"),
        }
        confidence = score_confidence(parts)
        if direction is Direction.BULLISH:
            stack = f"RSI={ind.rsi:.2f}>{threshold}, ROC={ind.roc:.3f}>0, price={ind.close:.2f}>EMA={ind.ema_fast:.2f}"
        else:
            stack = f"RSI={ind.rsi:.2f}<{bearish_rsi}, ROC={ind.roc:.3f}<0, price={ind.close:.2f}<EMA={ind.ema_fast:.2f}"
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
