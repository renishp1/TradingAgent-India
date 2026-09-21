"""Mean reversion v1. RANGE + oversold. LONG research only."""

from __future__ import annotations

from grow.data.schema import MarketSnapshot
from grow.strategies.models import Direction, StrategyContext, signal_fingerprint
from grow.strategies.signal import StrategySignal


class MeanReversionStrategy:
    name = "mean_reversion"
    version = "v1"

    def evaluate(self, snapshot: MarketSnapshot, context: StrategyContext) -> StrategySignal | None:
        ind = context.indicators
        if context.regime.label.value not in {"RANGE", "LOW_VOLATILITY", "UNKNOWN"}:
            return None
        if not ind.ready("rsi", "ema_fast", "atr"):
            return None
        assert ind.rsi is not None and ind.ema_fast is not None and ind.atr is not None
        oversold = float(context.params.get("rsi_oversold", 30))
        deviation = float(context.params.get("deviation", 0.01))
        if ind.rsi > oversold:
            return None
        if ind.close >= ind.ema_fast * (1.0 - deviation):
            return None
        entry = ind.close
        stop = round(entry - 1.5 * ind.atr, 2)
        target = round(ind.ema_fast, 2)
        if stop >= entry or target <= entry:
            return None
        risk = entry - stop
        reward = target - entry
        reason = (
            f"RANGE/mean-revert: RSI={ind.rsi:.2f}<={oversold}, "
            f"price={ind.close:.2f} below EMA={ind.ema_fast:.2f} by >{deviation:.2%}, "
            f"regime={context.regime.label.value}"
        )
        version = str(context.params.get("version", self.version))
        return StrategySignal(
            symbol=snapshot.symbol,
            strategy=self.name,
            direction=Direction.LONG.value,
            entry=round(entry, 2),
            stop=stop,
            target=target,
            confidence=0.45,
            timeframe=context.timeframe,
            reason=reason,
            as_of=snapshot.as_of,
            snapshot_id=snapshot.snapshot_id,
            signal_id=signal_fingerprint(
                symbol=snapshot.symbol.qualified(),
                strategy=self.name,
                timeframe=context.timeframe.value,
                as_of=snapshot.as_of.isoformat(),
                snapshot_id=snapshot.snapshot_id,
                strategy_version=version,
                direction=Direction.LONG.value,
            ),
            strategy_version=version,
            regime=context.regime.label.value,
            risk_reward=round(reward / risk, 2) if risk else None,
        )
