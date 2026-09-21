"""Reject malformed StrategySignals before they leave the engine."""

from __future__ import annotations

from grow.clock import IST
from grow.data.schema import Timeframe
from grow.errors import GrowConfigError
from grow.strategies.models import Direction
from grow.strategies.signal import StrategySignal

_FORBIDDEN_EXECUTION = frozenset({"LONG", "SHORT", "SELL", "BUY", "OPTION_SELL"})


def validate_signal(signal: StrategySignal, *, snapshot_id: str, allowed_symbols: tuple[str, ...]) -> None:
    if signal.direction in _FORBIDDEN_EXECUTION:
        raise GrowConfigError(f"{signal.direction} is execution language; 2B uses BULLISH/BEARISH")
    if signal.direction not in {Direction.BULLISH.value, Direction.BEARISH.value}:
        raise GrowConfigError(f"invalid research direction {signal.direction!r}")
    if signal.symbol.ticker not in allowed_symbols:
        raise GrowConfigError(f"symbol {signal.symbol.ticker} not in 2B universe")
    if signal.timeframe is not Timeframe.M15:
        raise GrowConfigError("2B signals must use primary timeframe M15")
    if signal.as_of.tzinfo is None or getattr(signal.as_of.tzinfo, "key", None) != "Asia/Kolkata":
        raise GrowConfigError("signal.as_of must be Asia/Kolkata")
    if signal.snapshot_id != snapshot_id:
        raise GrowConfigError("signal.snapshot_id does not match the snapshot")
    if not signal.strategy or not signal.strategy_version:
        raise GrowConfigError("strategy and strategy_version are required")
    if signal.entry <= 0 or signal.stop <= 0 or signal.target <= 0:
        raise GrowConfigError("entry/stop/target must be positive")
    if signal.direction == Direction.BULLISH.value:
        if not (signal.stop < signal.entry < signal.target):
            raise GrowConfigError("BULLISH requires stop < entry < target")
    else:
        if not (signal.target < signal.entry < signal.stop):
            raise GrowConfigError("BEARISH requires target < entry < stop")
    risk = abs(signal.entry - signal.stop)
    reward = abs(signal.target - signal.entry)
    if risk <= 0 or reward <= 0:
        raise GrowConfigError("risk and reward must be > 0")
    if signal.risk_reward is not None and signal.risk_reward <= 0:
        raise GrowConfigError("risk_reward must be > 0")
    if not 0.0 <= signal.confidence <= 1.0:
        raise GrowConfigError("confidence must be in [0, 1]")
    if not signal.signal_id:
        raise GrowConfigError("signal_id is required")
    _ = IST
