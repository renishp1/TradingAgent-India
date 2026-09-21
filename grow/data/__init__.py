"""Market data adapters — interface only.

Do not scrape NSE/BSE HTML from this package. A later milestone should use
licensed or officially supported feeds, reviewed independently of any fork.
"""

from grow.errors import GrowInterfaceNotImplemented


class DataHub:
    def quote(self, symbol: str) -> None:
        raise GrowInterfaceNotImplemented("grow.data", "milestone 2+")

    def option_chain(self, symbol: str) -> None:
        raise GrowInterfaceNotImplemented("grow.data", "milestone 2+")
