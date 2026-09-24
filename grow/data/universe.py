"""Cash-market data universe (Milestone 2A freeze).

This is a **seed**, not the official NSE circular. Replace it with an
exchange-sourced constituent file before attaching any licensed feed.

Trading universe (`grow.market.universe`) stays the small paper book.
Data universe is wider and is for research snapshots only.
"""

from __future__ import annotations

from grow.data.schema import InstrumentKind
from grow.types import Symbol

# Freeze date for this seed list. Not an official reconstitution.
UNIVERSE_FREEZE = "2026-09-21"

NIFTY50_EQUITIES: tuple[str, ...] = (
    "ADANIENT",
    "ADANIPORTS",
    "APOLLOHOSP",
    "ASIANPAINT",
    "AXISBANK",
    "BAJAJ-AUTO",
    "BAJAJFINSV",
    "BAJFINANCE",
    "BEL",
    "BHARTIARTL",
    "CIPLA",
    "COALINDIA",
    "DRREDDY",
    "EICHERMOT",
    "ETERNAL",
    "GRASIM",
    "HCLTECH",
    "HDFCBANK",
    "HDFCLIFE",
    "HEROMOTOCO",
    "HINDALCO",
    "HINDUNILVR",
    "ICICIBANK",
    "INDUSINDBK",
    "INFY",
    "ITC",
    "JIOFIN",
    "JSWSTEEL",
    "KOTAKBANK",
    "LT",
    "M&M",
    "MARUTI",
    "NESTLEIND",
    "NTPC",
    "ONGC",
    "POWERGRID",
    "RELIANCE",
    "SBILIFE",
    "SBIN",
    "SHRIRAMFIN",
    "SUNPHARMA",
    "TATACONSUM",
    "TATAMOTORS",
    "TATASTEEL",
    "TCS",
    "TECHM",
    "TITAN",
    "TRENT",
    "ULTRACEMCO",
    "WIPRO",
)

NIFTY_INDICES: tuple[str, ...] = (
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYIT",
    "SENSEX",
)

# Indices that live on BSE (everything else in NIFTY_INDICES is NSE).
BSE_INDICES: frozenset[str] = frozenset({"SENSEX"})


SELECTED_NSE_STOCKS: tuple[str, ...] = (
    "RELIANCE",
    "TCS",
    "HDFCBANK",
    "INFY",
    "ICICIBANK",
    "SBIN",
    "BHARTIARTL",
    "ITC",
    "LT",
    "HINDUNILVR",
)


def _kind(ticker: str) -> InstrumentKind:
    return InstrumentKind.INDEX if ticker in NIFTY_INDICES else InstrumentKind.EQUITY


def data_universe() -> tuple[Symbol, ...]:
    names = tuple(dict.fromkeys((*NIFTY_INDICES, *NIFTY50_EQUITIES)))
    return tuple(
        Symbol(ticker=name, exchange="BSE" if name in BSE_INDICES else "NSE") for name in names
    )


def is_in_data_universe(ticker: str) -> bool:
    name = ticker.strip().upper()
    return name in NIFTY50_EQUITIES or name in NIFTY_INDICES


def instrument_kind(ticker: str) -> InstrumentKind:
    return _kind(ticker.strip().upper())
