"""Deterministic BUY-only option fill model. Isolated from production fills."""

from __future__ import annotations

from datetime import datetime

from grow.backtest.costs import SlippageModel
from grow.backtest.models import SimulatedFill
from grow.options.models import OptionCandidate, OptionContract


class ExecutionSimulator:
    def __init__(self, slip: SlippageModel, *, quantity: int = 1, strict: bool = True) -> None:
        self.slip = slip
        self.quantity = quantity
        self.strict = strict

    def enter(self, candidate: OptionCandidate, *, as_of: datetime) -> SimulatedFill | str:
        if candidate.intent != "BUY":
            return "NON_BUY_INTENT"
        if candidate.option_type not in {"CE", "PE"}:
            return "INVALID_OPTION_TYPE"
        ask = candidate.ask
        bid = candidate.bid
        if ask is None or ask <= 0:
            return "UNKNOWN_QUOTE" if self.strict else "LTP_ONLY_REJECTED"
        if bid is not None and bid <= 0:
            return "NEGATIVE_BID"
        if bid is not None and bid > ask:
            return "CROSSED_QUOTE"
        fill, slip = self.slip.buy(ask)
        return SimulatedFill(
            side="BUY",
            price=fill,
            quantity=self.quantity,
            slippage=slip,
            timestamp=as_of,
            reference=ask,
            reason="ASK_PLUS_SLIPPAGE",
        )

    def exit(
        self,
        contract: OptionContract | None,
        *,
        as_of: datetime,
        reason: str,
        fallback_bid: float | None = None,
    ) -> SimulatedFill | str:
        bid = None
        if contract is not None:
            if contract.bid is not None and contract.ask is not None and contract.bid > contract.ask:
                return "CROSSED_QUOTE"
            bid = contract.bid
        if bid is None:
            bid = fallback_bid
        if bid is None or bid <= 0:
            return "NO_EXIT_QUOTE"
        fill, slip = self.slip.sell(bid)
        return SimulatedFill(
            side="SELL",
            price=fill,
            quantity=self.quantity,
            slippage=slip,
            timestamp=as_of,
            reference=bid,
            reason=reason,
        )
