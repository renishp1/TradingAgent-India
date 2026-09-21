"""Licensed-feed adapter — refuse-closed until a dedicated review.

Milestone 2A designs the types and the fixture source. It does **not**
attach NSE, BSE, vendor, or broker market data. Calling this module is a
bug, not a configuration option.
"""

from __future__ import annotations

from grow.errors import GrowInterfaceNotImplemented


class LicensedFeed:
    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise GrowInterfaceNotImplemented(
            "grow.data.licensed",
            "after the 2A architecture review accepts a named vendor",
        )

    def snapshot(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise GrowInterfaceNotImplemented("grow.data.licensed", "after 2A review")
