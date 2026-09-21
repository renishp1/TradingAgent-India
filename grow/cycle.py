"""One paper-trading cycle: research → (optional graph) → CEO → Risk Guard → paper.

This is the only supported orchestration path in milestone 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from grow.ceo.ceo import CEO
from grow.config import GrowConfig
from grow.market.research import MarketResearch
from grow.model_gateway.gateway import ModelGateway
from grow.paper.ledger import PaperLedger
from grow.risk.guard import RiskGuard
from grow.types import Fill, MarketBrief, RiskVerdict, TradeProposal
from tradingagents.graph.research_graph import ResearchBundle, ResearchGraph


@dataclass
class CycleReport:
    symbol: str
    brief: MarketBrief
    research: ResearchBundle | None
    proposal: TradeProposal
    verdict: RiskVerdict
    fill: Fill | None
    book: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "brief": self.brief.to_dict(),
            "research": None if self.research is None else self.research.to_dict(),
            "proposal": self.proposal.to_dict(),
            "verdict": self.verdict.to_dict(),
            "fill": None if self.fill is None else self.fill.to_dict(),
            "book": self.book,
        }


class GrowRuntime:
    def __init__(self, config: GrowConfig, clock=None, *, risk_secret: str | None = None) -> None:
        config.assert_safe()
        self.config = config
        self.clock = clock
        self.gateway = ModelGateway(config)
        self.market = MarketResearch(config, clock=clock)
        self.ceo = CEO(config, self.gateway, clock=clock)
        self.guard = RiskGuard(config, clock=clock, secret=risk_secret)
        self.ledger = PaperLedger(config, self.guard, clock=clock)
        self.graph = ResearchGraph(config) if config.tradingagents.enabled else None

    def run(self, ticker: str) -> CycleReport:
        brief = self.market.research(ticker)
        research = None
        notes = None
        if self.graph is not None:
            research = self.graph.propagate(ticker, brief.as_of.date().isoformat(), brief=brief)
            notes = research.trader_plan
        proposal = self.ceo.propose(brief, research_notes=notes)
        book = self.ledger.book
        verdict = self.guard.evaluate(
            proposal,
            brief,
            cash=book.cash,
            gross_notional=book.gross_notional,
            # Known limitation: this is lifetime realized-at-cost of the
            # in-memory book, not a trading-day P&L accumulator. True daily
            # P&L waits for the Milestone 2A valuation layer.
            daily_pnl=book.realized_pnl,
            symbol_notional=book.symbol_notional(proposal.symbol.ticker),
        )
        fill = None
        if verdict.approved:
            fill = self.ledger.submit(proposal, verdict.stamp)
        return CycleReport(
            symbol=ticker.upper(),
            brief=brief,
            research=research,
            proposal=proposal,
            verdict=verdict,
            fill=fill,
            book=self.ledger.book.snapshot(),
        )
