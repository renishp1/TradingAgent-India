"""Options desk — interface only.

F&O chains, greeks, expiry, and lot-size logic are deferred. Import is safe.
Calling the desk raises GrowInterfaceNotImplemented.
"""

from grow.errors import GrowInterfaceNotImplemented


class OptionsDesk:
    def chain(self, symbol: str) -> None:
        raise GrowInterfaceNotImplemented("grow.options", "milestone 2+")

    def greeks(self, symbol: str) -> None:
        raise GrowInterfaceNotImplemented("grow.options", "milestone 2+")
