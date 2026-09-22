"""Paper fill simulation. Deterministic for tests, configurable for experiments.

Fills use only a quote whose timestamp is at or before the fill timestamp.
Buy slippage is added. Sell slippage is subtracted. No broker is contacted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from grow.clock import IST
from grow.market_data.normalized.models import OptionQuoteView

DETERMINISTIC_FILL_MODEL = "paper.fill.deterministic.v1"
CONFIGURABLE_FILL_MODEL = "paper.fill.configurable.v1"
PRICE_SOURCES = frozenset({"LTP", "BID", "ASK", "MIDPOINT"})


@dataclass(frozen=True)
class FillPolicy:
    """Which quote and how much slippage a paper fill may use."""

    model: str
    deterministic: bool
    entry_source: str
    exit_source: str
    slippage_bps: float

    @property
    def version(self) -> str:
        return DETERMINISTIC_FILL_MODEL if self.deterministic else CONFIGURABLE_FILL_MODEL


@dataclass(frozen=True)
class FillSimulation:
    price: float
    reference: float
    slippage: float
    source: str
    quote_timestamp: datetime
    model_version: str
    slippage_bps: float


def policy_from_config(config) -> FillPolicy:
    paper = config.paper
    deterministic = paper.fill_model == "deterministic"
    if deterministic:
        return FillPolicy(
            model=paper.fill_model,
            deterministic=True,
            entry_source="LTP",
            exit_source="BID",
            slippage_bps=0.0,
        )
    bps = paper.slippage_bps
    if bps is None:
        bps = config.backtest.slippage_bps
    return FillPolicy(
        model=paper.fill_model,
        deterministic=False,
        entry_source=paper.entry_price_source,
        exit_source=paper.exit_price_source,
        slippage_bps=float(bps),
    )


def reference_price(quote: OptionQuoteView, source: str) -> float | None:
    """Approved paper price sources. Missing or crossed quotes do not invent a price."""
    if source not in PRICE_SOURCES:
        return None
    bid = quote.bid
    ask = quote.ask
    ltp = quote.ltp
    if bid is not None and ask is not None and bid > 0 and ask > 0 and bid > ask:
        return None
    if source == "LTP":
        return float(ltp) if ltp is not None and ltp > 0 else None
    if source == "BID":
        return float(bid) if bid is not None and bid > 0 else None
    if source == "ASK":
        return float(ask) if ask is not None and ask > 0 else None
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return round((float(bid) + float(ask)) / 2.0, 4)


def deterministic_exit_source(quote: OptionQuoteView) -> str | None:
    """3B sell-side mark: valid BID, else valid LTP."""
    if reference_price(quote, "BID") is not None:
        return "BID"
    if reference_price(quote, "LTP") is not None:
        return "LTP"
    return None


def simulate_fill(
    quote: OptionQuoteView,
    *,
    source: str,
    side: str,
    slippage_bps: float,
    as_of: datetime,
    model_version: str,
) -> FillSimulation | str:
    """Fill at ``as_of`` from a quote that is already known. Future quotes are refused."""
    quote_at = quote.quote_timestamp.astimezone(IST)
    fill_at = as_of.astimezone(IST)
    if quote_at > fill_at:
        return "FUTURE_PRICE"
    reference = reference_price(quote, source)
    if reference is None:
        if quote.bid is not None and quote.ask is not None and quote.bid > quote.ask:
            return "CROSSED_QUOTE"
        return "NO_FILL_QUOTE"
    slip = round(reference * float(slippage_bps) / 10_000.0, 4)
    if side == "BUY":
        price = round(reference + slip, 4)
    elif side == "SELL":
        price = round(max(0.05, reference - slip), 4)
    else:
        return "INVALID_SIDE"
    if price <= 0:
        return "NO_FILL_QUOTE"
    return FillSimulation(
        price=price,
        reference=reference,
        slippage=slip,
        source=source,
        quote_timestamp=quote_at,
        model_version=model_version,
        slippage_bps=float(slippage_bps),
    )
