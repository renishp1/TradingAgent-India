"""Quantitative strategy engine (Milestone 2B).

Deterministic StrategySignal generation from MarketSnapshot.
No options, no broker, no LLM, no execution.
"""

from grow.errors import GrowInterfaceNotImplemented
from grow.strategies.engine import StrategyEngine
from grow.strategies.models import StrategyContext, StrategyResult
from grow.strategies.registry import StrategyRegistry, default_registry
from grow.strategies.signal import StrategySignal


class StrategyBook:
    """Compatibility stub. Execution is forbidden. Use StrategyEngine.evaluate."""

    def list_strategies(self) -> list[str]:
        return list(default_registry().names())

    def run(self, name: str, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise GrowInterfaceNotImplemented(
            "grow.strategies.book.run",
            "StrategyEngine.evaluate — 2B does not execute",
        )


__all__ = [
    "StrategyBook",
    "StrategyContext",
    "StrategyEngine",
    "StrategyRegistry",
    "StrategyResult",
    "StrategySignal",
]
