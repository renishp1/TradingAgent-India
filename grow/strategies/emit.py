"""Build a research StrategySignal. Never an order."""

from __future__ import annotations

from typing import Any, Mapping

from grow.data.schema import MarketSnapshot
from grow.strategies.confidence import clamp
from grow.strategies.models import Direction, StrategyContext, signal_fingerprint
from grow.strategies.signal import StrategySignal


def research_signal(
    snapshot: MarketSnapshot,
    context: StrategyContext,
    *,
    name: str,
    direction: Direction,
    entry: float,
    stop: float,
    target: float,
    confidence: float,
    reason: str,
    extras: Mapping[str, Any] | None = None,
) -> StrategySignal | None:
    if direction not in (Direction.BULLISH, Direction.BEARISH):
        return None
    if direction is Direction.BULLISH and not (stop < entry < target):
        return None
    if direction is Direction.BEARISH and not (target < entry < stop):
        return None
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0 or reward <= 0:
        return None
    version = str(context.params.get("version", "v1"))
    bounded = round(clamp(confidence), 4)
    return StrategySignal(
        symbol=snapshot.symbol,
        strategy=name,
        direction=direction.value,
        entry=round(entry, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        confidence=bounded,
        timeframe=context.timeframe,
        reason=reason,
        as_of=snapshot.as_of,
        snapshot_id=snapshot.snapshot_id,
        signal_id=signal_fingerprint(
            symbol=snapshot.symbol.qualified(),
            strategy=name,
            timeframe=context.timeframe.value,
            as_of=snapshot.as_of.isoformat(),
            snapshot_id=snapshot.snapshot_id,
            strategy_version=version,
            direction=direction.value,
        ),
        strategy_version=version,
        regime=context.regime.label.value,
        risk_reward=round(reward / risk, 2),
        extras=extras,
    )


def atr_levels(entry: float, atr: float, direction: Direction, *, multiple: float = 1.5, rr: float = 2.0) -> tuple[float, float]:
    if direction is Direction.BULLISH:
        stop = entry - multiple * atr
        target = entry + rr * (entry - stop)
    else:
        stop = entry + multiple * atr
        target = entry - rr * (stop - entry)
    return round(stop, 2), round(target, 2)
