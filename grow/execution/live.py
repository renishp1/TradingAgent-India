"""Live brokerage surface — present only to fail closed.

Milestone 1 does not import Kite, Upstox, Dhan, or any other broker SDK.
Instantiating or calling anything in this module raises GrowLiveTradingDisabled.
"""

from __future__ import annotations

from grow.errors import GrowLiveTradingDisabled
from grow.execution.lock import assert_paper_compiled


def _refuse(action: str) -> None:
    assert_paper_compiled()
    raise GrowLiveTradingDisabled(
        f"{action} is not compiled into Grow milestone 1. Paper ledger only."
    )


class LiveBroker:
    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        _refuse("LiveBroker")

    def connect(self) -> None:
        _refuse("LiveBroker.connect")

    def place_order(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        _refuse("LiveBroker.place_order")

    def cancel_order(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        _refuse("LiveBroker.cancel_order")


def place_live_order(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
    _refuse("place_live_order")
