"""Momentum v1: RSI + ROC + price vs EMA. LONG research only."""

from __future__ import annotations

from grow.data.schema import MarketSnapshot
from grow.strategies.models import Direction, StrategyContext, signal_fingerprint
from grow.strategies.signal import StrategySignal


class MomentumStrategy:
    name = "momentum"
    version = "v1"

    def evaluate(self, snapshot: MarketSnapshot, context: StrategyContext) -> StrategySignal | None:
        ind = context.indicators
        if context.regime.label.value == "BEAR_TREND":
            return None
        if not ind.ready("rsi", "roc", "ema_fast", "atr"):
            return None
        assert ind.rsi is not None and ind.roc is not None
        assert ind.ema_fast is not None and ind.atr is not None
        threshold = float(context.params.get("rsi_threshold", 55))
        if not (ind.rsi > threshold and ind.roc > 0 and ind.close > ind.ema_fast):
            return None
        entry = ind.close
        stop = round(entry - 1.5 * ind.atr, 2)
        if stop >= entry:
            return None
        risk = entry - stop
        target = round(entry + 2.0 * risk, 2)
        confirms = 1 + (1 if ind.rsi > threshold + 5 else 0) + (1 if ind.roc > 0.2 else 0)
        confidence = round(min(0.85, 0.30 + 0.15 * confirms), 2)
        reason = (
            f"RSI={ind.rsi:.2f}>{threshold}, ROC={ind.roc:.3f}>0, "
            f"price={ind.close:.2f}>EMA={ind.ema_fast:.2f}, regime={context.regime.label.value}"
        )
        version = str(context.params.get("version", self.version))
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
            risk_reward=round(2.0, 2),
        )
