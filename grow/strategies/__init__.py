"""Strategy library — interface only."""

from grow.errors import GrowInterfaceNotImplemented


class StrategyBook:
    def list_strategies(self) -> list[str]:
        return []

    def run(self, name: str, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise GrowInterfaceNotImplemented("grow.strategies", "milestone 2+")
