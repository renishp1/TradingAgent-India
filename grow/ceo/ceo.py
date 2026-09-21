"""CEO interface.

The CEO reads research and produces a TradeProposal. It cannot stamp risk,
cannot fill, and cannot see a broker. The only legal way a CEO proposal
becomes a position is: Risk Guard stamp → paper ledger.

Architecture Review #1: NSE cash book is long-only. OPEN+SELL is not a
policy the CEO may emit. Stop/target percentages are probe placeholders,
not a strategy engine.
"""

from __future__ import annotations

from uuid import uuid4

from grow.clock import Clock, SystemClock
from grow.config import GrowConfig
from grow.model_gateway.gateway import ModelGateway
from grow.types import Intent, MarketBrief, Side, TradeProposal, Venue

_MAX_PROBE_QTY = 5
# Placeholder probe band. Strategy engine replaces this. Not ATR / regime.
PROBE_STOP_PCT = 0.02
PROBE_TAKE_PCT = 0.03


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
        requested = str(structured.get("side", "BUY")).upper()
        # Cash long-only: OPEN is always BUY. SELL is reserved for flatten.
        side = Side.BUY
        raw_qty = int(structured.get("quantity", 1) or 1)
        quantity = max(1, min(raw_qty, _MAX_PROBE_QTY))
        price = brief.last_price
        stop = round(price * (1.0 - PROBE_STOP_PCT), 2)
        take = round(price * (1.0 + PROBE_TAKE_PCT), 2)
        thesis = str(structured.get("thesis") or "CEO proposal")
        if requested != "BUY":
            thesis = f"{thesis} | cash policy: model side={requested} ignored; OPEN is long-only"
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
            extras={
                "model_provider": self.gateway.name,
                "model_text": response.text,
                "model_requested_side": requested,
                "position_policy": "cash_long_only",
                "stop_source": "probe_placeholder",
                "stop_pct": PROBE_STOP_PCT,
                "take_pct": PROBE_TAKE_PCT,
            },
        )

    def _prompt(self, brief: MarketBrief, research_notes: str | None) -> str:
        return (
            "You are Grow's CEO. Paper-trading only. Indian cash market, long-only.\n"
            f"Symbol: {brief.symbol.qualified()}\n"
            f"Last: {brief.last_price} {brief.currency}\n"
            f"Session: {brief.session.value}\n"
            f"Regime: {brief.regime.value}\n"
            f"Notes: {'; '.join(brief.notes)}\n"
            f"Research: {research_notes or 'none'}\n"
            "Return a conservative OPEN long probe (BUY). Never request a live venue. "
            "Never request a short (OPEN+SELL)."
        )
