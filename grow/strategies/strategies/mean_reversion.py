"""Mean reversion v1. Oversold BULLISH / overbought BEARISH. Not an order."""

from __future__ import annotations

from grow.data.schema import MarketSnapshot
from grow.strategies.confidence import format_parts, regime_alignment, score_confidence, unit
from grow.strategies.emit import research_signal
from grow.strategies.models import Direction, MarketRegime, StrategyContext
from grow.strategies.signal import StrategySignal

_RANGE_OK = {MarketRegime.RANGE, MarketRegime.LOW_VOLATILITY, MarketRegime.UNKNOWN}


class MeanReversionStrategy:
    name = "mean_reversion"
    version = "v1"

    def evaluate(self, snapshot: MarketSnapshot, context: StrategyContext) -> StrategySignal | None:
        ind = context.indicators
        if context.regime.label not in _RANGE_OK:
            return None
        if not ind.ready("rsi", "ema_fast", "atr"):
            return None
        assert ind.rsi is not None and ind.ema_fast is not None and ind.atr is not None
        oversold = float(context.params.get("rsi_oversold", 30))
        overbought = float(context.params.get("rsi_overbought", 70))
        deviation = float(context.params.get("deviation", 0.01))
        if ind.rsi <= oversold and ind.close < ind.ema_fast * (1.0 - deviation):
            return self._emit(snapshot, context, Direction.BULLISH, oversold, overbought)
        if ind.rsi >= overbought and ind.close > ind.ema_fast * (1.0 + deviation):
            return self._emit(snapshot, context, Direction.BEARISH, oversold, overbought)
        return None

    def _emit(self, snapshot, context, direction: Direction, oversold: float, overbought: float) -> StrategySignal | None:
        ind = context.indicators
        assert ind.rsi is not None and ind.ema_fast is not None and ind.atr is not None
        if direction is Direction.BULLISH:
            stop = ind.close - 1.5 * ind.atr
            stack = f"oversold: RSI={ind.rsi:.2f}<={oversold}, price={ind.close:.2f} below EMA={ind.ema_fast:.2f}"
        else:
            stop = ind.close + 1.5 * ind.atr
            stack = f"overbought: RSI={ind.rsi:.2f}>={overbought}, price={ind.close:.2f} above EMA={ind.ema_fast:.2f}"
        target = ind.ema_fast
        parts = {
            "trend_alignment": unit(ind.close - ind.ema_fast, 0.03 * ind.ema_fast),
            "indicator_strength": unit(ind.rsi - 50.0, 35.0),
            "structure_confirmation": unit(ind.rsi - 50.0, 25.0),
            "regime_alignment": regime_alignment(direction, context.regime.label, style="reversion"),
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
