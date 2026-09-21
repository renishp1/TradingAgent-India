"""Sequential research graph.

Upstream TradingAgents uses LangGraph with debate cycles. Grow's foundation
graph is a deterministic pipeline that returns a ResearchBundle the CEO can
read. It does not place trades and does not call Risk Guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from grow.config import GrowConfig
from grow.types import MarketBrief
from tradingagents.agents.protocols import AgentNote
from tradingagents.default_config import DEFAULT_CONFIG


def _note(role: str, headline: str, detail: str, bias: str) -> AgentNote:
    return AgentNote(role=role, headline=headline, detail=detail, bias=bias)


@dataclass
class ResearchBundle:
    ticker: str
    as_of: str
    notes: list[AgentNote] = field(default_factory=list)
    trader_plan: str = ""
    bull_case: str = ""
    bear_case: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "as_of": self.as_of,
            "trader_plan": self.trader_plan,
            "bull_case": self.bull_case,
            "bear_case": self.bear_case,
            "notes": [
                {"role": n.role, "headline": n.headline, "detail": n.detail, "bias": n.bias}
                for n in self.notes
            ],
        }


class ResearchGraph:
    def __init__(self, config: GrowConfig | None = None, extra: dict | None = None) -> None:
        self.config = config
        self.extra = {**DEFAULT_CONFIG, **(extra or {})}

    def propagate(self, ticker: str, as_of: str, brief: MarketBrief | None = None) -> ResearchBundle:
        symbol = ticker.strip().upper()
        price = None if brief is None else brief.last_price
        regime = None if brief is None else brief.regime.value
        session = None if brief is None else brief.session.value

        fundamentals = _note(
            "fundamentals",
            "Stub fundamentals",
            "No Screener.in / filings pull in milestone 1. Treat as missing evidence.",
            "neutral",
        )
        sentiment = _note(
            "sentiment",
            "Stub sentiment",
            "No social or news sentiment feed is attached.",
            "neutral",
        )
        news = _note(
            "news",
            "Stub news",
            "No RSS / headline vendor. Do not invent catalysts.",
            "neutral",
        )
        technical = _note(
            "technical",
            "Stub technicals",
            f"Last stub price={price} regime={regime} session={session}. Not OHLCV.",
            "neutral",
        )
        analysts = [fundamentals, sentiment, news, technical]

        rounds = 1
        if self.config is not None:
            rounds = max(1, self.config.tradingagents.max_debate_rounds)
        else:
            rounds = max(1, int(self.extra.get("max_debate_rounds", 1)))

        bull = _note(
            "bull_researcher",
            "Bull case (constrained)",
            f"{rounds} debate round(s): upside exists only as a paper probe, not a forecast.",
            "bull",
        )
        bear = _note(
            "bear_researcher",
            "Bear case (constrained)",
            "Missing live data is itself a reason to stay small or stand aside.",
            "bear",
        )
        trader = _note(
            "trader",
            "Paper probe only",
            "Recommend at most a size-one paper OPEN if Risk Guard agrees; else WAIT.",
            "neutral",
        )
        notes = analysts + [bull, bear, trader]
        return ResearchBundle(
            ticker=symbol,
            as_of=as_of,
            notes=notes,
            trader_plan=trader.detail,
            bull_case=bull.detail,
            bear_case=bear.detail,
        )
