"""Breakout v1. Rolling high uses previous completed bars only."""

from __future__ import annotations

from grow.data.schema import MarketSnapshot
from grow.strategies.models import Direction, StrategyContext, signal_fingerprint
from grow.strategies.signal import StrategySignal


class BreakoutStrategy:
    name = "breakout"
    version = "v1"

    def evaluate(self, snapshot: MarketSnapshot, context: StrategyContext) -> StrategySignal | None:
        ind = context.indicators
        if context.regime.label.value in {"RANGE", "BEAR_TREND"}:
            return None
        if not ind.ready("rolling_high", "rolling_low", "atr"):
            return None
        assert ind.rolling_high is not None and ind.rolling_low is not None and ind.atr is not None
        if ind.close <= ind.rolling_high:
            return None
        if ind.body is not None and ind.body <= 0:
            return None
        entry = ind.close
        stop = round(min(ind.rolling_low, entry - ind.atr), 2)
        if stop >= entry:
            return None
        risk = entry - stop
        target = round(entry + 2.0 * risk, 2)
        reason = (
            f"close={ind.close:.2f} > prior {context.params.get('lookback', 20)}-bar high "
            f"{ind.rolling_high:.2f} (current bar excluded), regime={context.regime.label.value}"
        )
        version = str(context.params.get("version", self.version))
        return StrategySignal(
            symbol=snapshot.symbol,
            strategy=self.name,
            direction=Direction.LONG.value,
            entry=round(entry, 2),
            stop=stop,
            target=target,
            confidence=0.55,
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
