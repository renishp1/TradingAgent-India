"""Strategy library.

Milestone 2A freezes the StrategySignal type. The book itself is 2B —
quant calculations over MarketSnapshot, not LLM-as-trader.
"""

from grow.errors import GrowInterfaceNotImplemented
from grow.strategies.signal import StrategySignal


class StrategyBook:
    def list_strategies(self) -> list[str]:
        return []

    def run(self, name: str, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise GrowInterfaceNotImplemented("grow.strategies", "milestone 2B")


__all__ = ["StrategyBook", "StrategySignal"]
