"""Mean reversion v1. Oversold BULLISH / overbought BEARISH. Not an order."""

from __future__ import annotations

from grow.data.schema import MarketSnapshot
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
            stop = ind.close - 1.5 * ind.atr
            target = ind.ema_fast
            return research_signal(
                snapshot,
                context,
                name=self.name,
                direction=Direction.BULLISH,
                entry=ind.close,
                stop=stop,
                target=target,
                confidence=0.45,
                reason=(
                    f"oversold: RSI={ind.rsi:.2f}<={oversold}, "
                    f"price={ind.close:.2f} below EMA={ind.ema_fast:.2f}, "
                    f"regime={context.regime.label.value}"
                ),
            )
        if ind.rsi >= overbought and ind.close > ind.ema_fast * (1.0 + deviation):
            stop = ind.close + 1.5 * ind.atr
            target = ind.ema_fast
            return research_signal(
                snapshot,
                context,
                name=self.name,
                direction=Direction.BEARISH,
                entry=ind.close,
                stop=stop,
                target=target,
                confidence=0.45,
                reason=(
                    f"overbought: RSI={ind.rsi:.2f}>={overbought}, "
                    f"price={ind.close:.2f} above EMA={ind.ema_fast:.2f}, "
                    f"regime={context.regime.label.value}"
                ),
            )
        return None
