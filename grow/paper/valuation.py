"""Paper mark-to-market. BID preferred, LTP fallback. Never ASK. Never fabricate."""

from __future__ import annotations

from datetime import datetime

from grow.live_data.models import LiveSnapshot
from grow.options.models import OptionContract


def find_contract(snapshot: LiveSnapshot, *, underlying: str, expiry, strike: float, option_type: str) -> OptionContract | None:
    chain = snapshot.chains.get(underlying)
    if chain is None:
        return None
    want = str(option_type).upper()
    for contract in chain.contracts:
        if (
            contract.underlying == underlying
            and contract.expiry == expiry
            and abs(contract.strike - strike) < 1e-9
            and contract.option_type.value == want
        ):
            return contract
    return None


def mark_from_contract(contract: OptionContract) -> tuple[float, str] | None:
    """Sell-to-close valuation: valid BID, else valid LTP. Never ASK."""
    if contract.bid is not None and contract.bid > 0:
        return float(contract.bid), "BID"
    if contract.last_price is not None and contract.last_price > 0:
        return float(contract.last_price), "LTP"
    return None


def unrealized_gross(*, mark_price: float, entry_price: float, quantity: int) -> float:
    return round((mark_price - entry_price) * quantity, 4)


def quote_usable(contract: OptionContract, *, last_valued_at: datetime | None) -> str | None:
    if last_valued_at is not None and contract.timestamp < last_valued_at:
        return "VALUATION_NOT_MONOTONIC"
    return None
