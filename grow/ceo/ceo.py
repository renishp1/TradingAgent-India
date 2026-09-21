"""CEO interface.

The CEO reads research and produces a TradeProposal. It cannot stamp risk,
cannot fill, and cannot see a broker. The only legal way a CEO proposal
becomes a position is: Risk Guard stamp → paper ledger.
"""

from __future__ import annotations

from uuid import uuid4

from grow.clock import Clock, SystemClock
from grow.config import GrowConfig
from grow.model_gateway.gateway import ModelGateway
from grow.types import Intent, MarketBrief, Side, TradeProposal, Venue

_MAX_PROBE_QTY = 5


class CEO:
    def __init__(
        self,
        config: GrowConfig,
        gateway: ModelGateway,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.gateway = gateway
        self.clock = clock or SystemClock()

    def propose(self, brief: MarketBrief, research_notes: str | None = None) -> TradeProposal:
        prompt = self._prompt(brief, research_notes)
        response = self.gateway.complete("ceo.propose", prompt, deep=True)
        structured = response.structured
        side = Side.BUY if str(structured.get("side", "BUY")).upper() == "BUY" else Side.SELL
        raw_qty = int(structured.get("quantity", 1) or 1)
        quantity = max(1, min(raw_qty, _MAX_PROBE_QTY))
        price = brief.last_price
        if side is Side.BUY:
            stop = round(price * 0.98, 2)
            take = round(price * 1.03, 2)
        else:
            stop = round(price * 1.02, 2)
            take = round(price * 0.97, 2)
        thesis = str(structured.get("thesis") or "CEO proposal")
        if research_notes:
            thesis = f"{thesis} | research: {research_notes[:180]}"
        confidence = float(structured.get("confidence", 0.4))
        confidence = min(max(confidence, 0.0), 1.0)
        return TradeProposal(
            proposal_id=f"ceo-{brief.symbol.ticker}-{uuid4().hex[:8]}",
            symbol=brief.symbol,
            side=side,
            intent=Intent.OPEN,
            quantity=quantity,
            limit_price=price,
            stop_loss=stop,
            take_profit=take,
            thesis=thesis,
            confidence=confidence,
            venue=Venue.PAPER,
            created_at=self.clock.now(),
            notional=round(quantity * price, 2),
            extras={"model_provider": self.gateway.name, "model_text": response.text},
        )

    def _prompt(self, brief: MarketBrief, research_notes: str | None) -> str:
        return (
            "You are Grow's CEO. Paper-trading only. Indian cash market.\n"
            f"Symbol: {brief.symbol.qualified()}\n"
            f"Last: {brief.last_price} {brief.currency}\n"
            f"Session: {brief.session.value}\n"
            f"Regime: {brief.regime.value}\n"
            f"Notes: {'; '.join(brief.notes)}\n"
            f"Research: {research_notes or 'none'}\n"
            "Return a conservative OPEN probe. Never request a live venue."
        )
