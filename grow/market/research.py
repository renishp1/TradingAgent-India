"""Market Research interface.

Milestone 1 returns a deterministic stub brief. It does not scrape NSE, BSE,
or news sites. Real data adapters are a later milestone and must be reviewed
independently — existing Indian forks rely on fragile public pages.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from grow.clock import IST, Clock, SystemClock
from grow.config import GrowConfig
from grow.market.session import SessionCalendar
from grow.types import MarketBrief, Regime, Symbol


def _stable_price(ticker: str, as_of: datetime) -> float:
    seed = f"{ticker}:{as_of.date().isoformat()}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    basis = 400 + (int(digest[:6], 16) % 3600)
    noise = (int(digest[6:10], 16) % 100) / 100.0
    return round(basis + noise, 2)


def _stable_regime(ticker: str, as_of: datetime) -> Regime:
    digest = hashlib.sha256(f"regime:{ticker}:{as_of.date()}".encode()).hexdigest()
    choices = (
        Regime.TRENDING_UP,
        Regime.TRENDING_DOWN,
        Regime.RANGING,
        Regime.HIGH_VOLATILITY,
    )
    return choices[int(digest[:2], 16) % len(choices)]


class MarketResearch:
    """Produces MarketBrief objects. Never produces trades."""

    def __init__(self, config: GrowConfig, clock: Clock | None = None) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self.calendar = SessionCalendar(config.market, clock=self.clock)

    def is_listed(self, ticker: str) -> bool:
        return ticker.strip().upper() in self.config.market.universe

    def research(self, ticker: str, as_of: datetime | None = None) -> MarketBrief:
        symbol = Symbol(ticker=ticker, exchange=self.config.market.exchange)
        moment = (as_of or self.clock.now()).astimezone(IST)
        session = self.calendar.state(moment)
        notes = [
            "Stub quote. Not a live NSE/BSE feed.",
            f"Universe listed: {self.is_listed(symbol.ticker)}",
            f"Session={session.value}",
        ]
        regime = _stable_regime(symbol.ticker, moment)
        if regime is Regime.HIGH_VOLATILITY:
            notes.append("High-volatility regime flag — Risk Guard will block new entries.")
        return MarketBrief(
            symbol=symbol,
            as_of=moment,
            session=session,
            last_price=_stable_price(symbol.ticker, moment),
            currency=self.config.market.currency,
            regime=regime,
            notes=tuple(notes),
            source="grow.market.stub",
            extras={"universe": list(self.config.market.universe)},
        )
