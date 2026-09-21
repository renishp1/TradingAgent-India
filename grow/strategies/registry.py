"""Strategy registration. No if/elif engine switches."""

from __future__ import annotations

from grow.errors import GrowConfigError
from grow.strategies.base import Strategy


class StrategyRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        name = strategy.name
        if name in self._items:
            raise GrowConfigError(f"duplicate strategy registration: {name}")
        self._items[name] = strategy

    def get(self, name: str) -> Strategy:
        if name not in self._items:
            raise GrowConfigError(f"unknown strategy {name!r}")
        return self._items[name]

    def names(self) -> tuple[str, ...]:
        return tuple(self._items.keys())

    def all(self) -> tuple[Strategy, ...]:
        return tuple(self._items.values())


def default_registry() -> StrategyRegistry:
    from grow.strategies.strategies.breakout import BreakoutStrategy
    from grow.strategies.strategies.mean_reversion import MeanReversionStrategy
    from grow.strategies.strategies.momentum import MomentumStrategy
    from grow.strategies.strategies.trend import EmaTrendStrategy

    registry = StrategyRegistry()
    registry.register(EmaTrendStrategy())
    registry.register(MomentumStrategy())
    registry.register(BreakoutStrategy())
    registry.register(MeanReversionStrategy())
    return registry
