"""Index options research (Milestone 2C).

Fixture chains only. BUY CE / BUY PE candidates. No execution.
OptionsDesk.chain still refuses vendor/live access.
"""

from grow.errors import GrowInterfaceNotImplemented
from grow.options.engine import IndexOptionsEngine
from grow.options.fixture import FixtureOptionChain, open_option_source
from grow.options.models import DecisionStatus, OptionCandidate, OptionsDecision
from grow.options.source import OptionChainSource


class OptionsDesk:
    def chain(self, symbol: str) -> None:
        raise GrowInterfaceNotImplemented("grow.options.desk.chain", "licensed vendor after 2C review")

    def greeks(self, symbol: str) -> None:
        raise GrowInterfaceNotImplemented("grow.options.desk.greeks", "licensed vendor after 2C review")

    def buy(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise GrowInterfaceNotImplemented("grow.options.desk.buy", "never in 2C")

    def sell(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise GrowInterfaceNotImplemented("grow.options.desk.sell", "never — selling is forbidden")


__all__ = [
    "DecisionStatus",
    "FixtureOptionChain",
    "IndexOptionsEngine",
    "OptionCandidate",
    "OptionChainSource",
    "OptionsDecision",
    "OptionsDesk",
    "open_option_source",
]
