"""EMA trend v1. Underlying LONG research signal only."""

from __future__ import annotations

from grow.data.schema import MarketSnapshot
from grow.strategies.models import Direction, StrategyContext, signal_fingerprint
from grow.strategies.signal import StrategySignal


class EmaTrendStrategy:
    name = "ema_trend"
    version = "v1"

    def evaluate(self, snapshot: MarketSnapshot, context: StrategyContext) -> StrategySignal | None:
        ind = context.indicators
        if context.regime.label.value in {"BEAR_TREND", "RANGE"}:
            return None
        if not ind.ready("ema_fast", "ema_slow", "ema_fast_slope", "atr"):
            return None
        assert ind.ema_fast is not None and ind.ema_slow is not None
        assert ind.ema_fast_slope is not None and ind.atr is not None
        if not (ind.ema_fast > ind.ema_slow and ind.ema_fast_slope > 0 and ind.close > ind.ema_fast):
            return None
        entry = ind.close
        stop = round(min(ind.ema_fast, entry - 1.5 * ind.atr), 2)
        if stop >= entry:
            return None
        risk = entry - stop
        target = round(entry + 2.0 * risk, 2)
        confirms = 3
        confidence = round(min(0.85, 0.35 + 0.15 * confirms), 2)
        reason = (
            f"EMA{context.params.get('fast_period', 20)}={ind.ema_fast:.2f} > "
            f"EMA{context.params.get('slow_period', 50)}={ind.ema_slow:.2f}, "
            f"price={ind.close:.2f} above fast EMA, slope={ind.ema_fast_slope:.4f}, "
            f"regime={context.regime.label.value}"
        )
        version = str(context.params.get("version", self.version))
        signal_id = signal_fingerprint(
            symbol=snapshot.symbol.qualified(),
            strategy=self.name,
            timeframe=context.timeframe.value,
            as_of=snapshot.as_of.isoformat(),
            snapshot_id=snapshot.snapshot_id,
            strategy_version=version,
            direction=Direction.LONG.value,
        )
        return StrategySignal(
            symbol=snapshot.symbol,
            strategy=self.name,
            direction=Direction.LONG.value,
            entry=round(entry, 2),
            stop=stop,
            target=target,
            confidence=confidence,
            timeframe=context.timeframe,
            reason=reason,
            as_of=snapshot.as_of,
            snapshot_id=snapshot.snapshot_id,
            signal_id=signal_id,
            strategy_version=version,
            regime=context.regime.label.value,
            risk_reward=round(2.0, 2),
        )
