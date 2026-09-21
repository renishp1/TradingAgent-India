"""Corporate-action handling — identity in 2A, real adjustments later.

Fixture bars are unadjusted synthetic prices. A licensed feed must not be
attached until splits, bonuses, and dividends have an explicit adjuster.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from grow.data.schema import BarSeries
from grow.errors import GrowInterfaceNotImplemented


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    ex_date: date
    kind: str
    factor: float
    extras: dict[str, Any] | None = None


class IdentityAdjuster:
    """2A default: do not rewrite fixture bars."""

    name = "grow.data.actions.identity"

    def apply(self, series: BarSeries, actions: tuple[CorporateAction, ...]) -> BarSeries:
        if actions:
            raise GrowInterfaceNotImplemented(
                "grow.data.actions.adjust",
                "a licensed feed with a real corporate-action file",
            )
        return series


class LicensedActions:
    def load(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise GrowInterfaceNotImplemented("grow.data.actions.licensed", "after 2A review")
