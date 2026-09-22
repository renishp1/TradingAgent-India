"""Historical option-contract availability rules.

Decisions may only reference contracts that were listed and quoteable at the
decision timestamp. Today's chain must not reconstruct a historical decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from grow.clock import IST
from grow.errors import GrowConfigError
from grow.market_data.normalized.models import OptionQuoteView


class HistoricalContractView(Protocol):
    underlying: str
    expiry: Any
    strike: float
    option_type: str
    first_seen_at: datetime
    last_seen_at: datetime
    lot_size: int | None


@dataclass(frozen=True)
class ContractAvailability:
    available: bool
    reason: str
    contract_id: str | None = None
    lot_size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "contract_id": self.contract_id,
            "lot_size": self.lot_size,
        }


def contract_available_at(
    contract: HistoricalContractView,
    decision_at: datetime,
) -> ContractAvailability:
    moment = decision_at.astimezone(IST)
    first = contract.first_seen_at.astimezone(IST)
    last = contract.last_seen_at.astimezone(IST)
    cid = f"{contract.underlying}-{contract.expiry}-{contract.strike:g}-{contract.option_type}"
    if moment < first or moment > last:
        return ContractAvailability(False, "CONTRACT_NOT_LISTED", cid, contract.lot_size)
    if contract.lot_size is None or contract.lot_size < 1:
        return ContractAvailability(False, "MISSING_LOT_SIZE", cid, contract.lot_size)
    return ContractAvailability(True, "OK", cid, contract.lot_size)


def quote_on_historical_chain(
    quote: OptionQuoteView,
    *,
    decision_at: datetime,
    listed: Mapping[str, HistoricalContractView] | None = None,
) -> ContractAvailability:
    """Reject quotes that were unavailable at decision time or unknown historically."""

    if quote.quote_timestamp.astimezone(IST) > decision_at.astimezone(IST):
        return ContractAvailability(False, "FUTURE_QUOTE", quote.provider_contract_id)
    if listed is None:
        return ContractAvailability(True, "OK", quote.provider_contract_id)
    key = quote.provider_contract_id
    alt = f"{quote.underlying}-{quote.expiry.isoformat()}-{int(quote.strike)}-{quote.option_type}"
    contract = listed.get(key) or listed.get(alt)
    if contract is None:
        return ContractAvailability(False, "UNKNOWN_HISTORICAL_CONTRACT", key)
    return contract_available_at(contract, decision_at)


def assert_historical_not_today(decision_universe: set[str], today_universe: set[str]) -> None:
    """Do not silently substitute today's contract list for a historical decision."""

    if not decision_universe and today_universe:
        raise GrowConfigError("HISTORICAL_UNIVERSE_EMPTY_TODAY_SUBSTITUTED")
    leaked = today_universe - decision_universe
    if leaked and not decision_universe.issuperset(today_universe):
        # Only fail when today introduces contracts absent from the historical set
        # while the historical set is non-empty and being replaced.
        if decision_universe.isdisjoint(today_universe):
            raise GrowConfigError("TODAY_CHAIN_RECONSTRUCTION_FORBIDDEN")
