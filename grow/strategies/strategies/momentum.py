"""Momentum v1. BULLISH and BEARISH research. Not an order."""

from __future__ import annotations

from grow.data.schema import MarketSnapshot
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
            stop, target = atr_levels(ind.close, ind.atr, Direction.BULLISH)
            return research_signal(
                snapshot,
                context,
                name=self.name,
                direction=Direction.BULLISH,
                entry=ind.close,
                stop=stop,
                target=target,
                confidence=0.65,
                reason=(
                    f"RSI={ind.rsi:.2f}>{threshold}, ROC={ind.roc:.3f}>0, "
                    f"price={ind.close:.2f}>EMA={ind.ema_fast:.2f}, regime={regime.value}"
                ),
            )
        if bear and regime is not MarketRegime.BULL_TREND:
            stop, target = atr_levels(ind.close, ind.atr, Direction.BEARISH)
            return research_signal(
                snapshot,
                context,
                name=self.name,
                direction=Direction.BEARISH,
                entry=ind.close,
                stop=stop,
                target=target,
                confidence=0.65,
                reason=(
                    f"RSI={ind.rsi:.2f}<{bearish_rsi}, ROC={ind.roc:.3f}<0, "
                    f"price={ind.close:.2f}<EMA={ind.ema_fast:.2f}, regime={regime.value}"
                ),
            )
        return None
