"""TrueData symbol parsing. Provider symbols are not canonical contract IDs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from grow.errors import GrowConfigError

# Vendor index labels → canonical Grow symbols. Not an allow-list.
INDEX_ALIASES = {
    "NIFTY 50": "NIFTY",
    "NIFTY50": "NIFTY",
    "NIFTY": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "NIFTY MID SELECT": "MIDCPNIFTY",
    "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "FINNIFTY": "FINNIFTY",
}

_OPTION = re.compile(r"^([A-Z]+)(\d{6})(\d+)(CE|PE)$")


@dataclass(frozen=True)
class ParsedInstrument:
    provider_symbol: str
    canonical_symbol: str
    instrument_type: str
    expiry: date | None
    strike: float | None
    option_type: str | None

    def canonical_contract_id(self) -> str | None:
        if self.instrument_type != "INDEX_OPTION" or self.expiry is None or self.strike is None or self.option_type is None:
            return None
        return f"{self.canonical_symbol}-{self.expiry.isoformat()}-{int(self.strike)}-{self.option_type}"


def canonical_underlying(raw: str) -> str:
    name = " ".join(str(raw or "").strip().upper().split())
    if not name:
        raise GrowConfigError("UNKNOWN_INSTRUMENT")
    if name in INDEX_ALIASES:
        return INDEX_ALIASES[name]
    compact = name.replace(" ", "")
    if compact in INDEX_ALIASES:
        return INDEX_ALIASES[compact]
    return compact


def parse_provider_symbol(raw: str) -> ParsedInstrument:
    text = str(raw or "").strip()
    if not text:
        raise GrowConfigError("UNKNOWN_INSTRUMENT")
    option = _OPTION.match(text.replace(" ", "").upper())
    if option is None:
        underlying = canonical_underlying(text)
        return ParsedInstrument(
            provider_symbol=text,
            canonical_symbol=underlying,
            instrument_type="INDEX",
            expiry=None,
            strike=None,
            option_type=None,
        )
    ticker, yymmdd, strike_s, kind = option.groups()
    underlying = canonical_underlying(ticker)
    year = 2000 + int(yymmdd[0:2])
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    try:
        expiry = date(year, month, day)
    except ValueError as exc:
        raise GrowConfigError(f"INVALID_EXPIRY:{text}") from exc
    strike = float(strike_s)
    if strike <= 0:
        raise GrowConfigError(f"INVALID_STRIKE:{text}")
    return ParsedInstrument(
        provider_symbol=text,
        canonical_symbol=underlying,
        instrument_type="INDEX_OPTION",
        expiry=expiry,
        strike=strike,
        option_type=kind,
    )


def parse_tick_fields(raw: Any) -> dict[str, Any]:
    """Map documented TrueData L1 fields onto a vendor-neutral dict.

    CSV order (docs): Symbol, Date-Time, LTP, LTQ, ATP, TTQ, Open, High, Low,
    Prev Close, OI, Prev OI, Turnover, Tag, Tick Sequence, Bid, BidQty, Ask, AskQty.
    Missing bid/ask stay None. Never fabricated.
    """
    if isinstance(raw, Mapping):
        symbol = str(
            raw.get("symbol")
            or raw.get("Symbol")
            or raw.get("provider_symbol")
            or ""
        )
        symbol_id = raw.get("symbol_id") or raw.get("symbolid") or raw.get("SymbolId")
        ts = raw.get("timestamp") or raw.get("Date-Time") or raw.get("ltt") or raw.get("event_time")
        seq = raw.get("sequence") or raw.get("tick_sequence") or raw.get("Tick Sequence No")
        trade = raw.get("trade")
        if isinstance(trade, (list, tuple)):
            return parse_tick_fields(list(trade))
        return {
            "provider_symbol": symbol,
            "symbol_id": None if symbol_id in (None, "") else str(symbol_id),
            "timestamp": ts,
            "ltp": _num(raw.get("ltp") if "ltp" in raw else raw.get("LTP")),
            "bid": _num(raw.get("bid") if "bid" in raw else raw.get("Bid")),
            "ask": _num(raw.get("ask") if "ask" in raw else raw.get("Ask")),
            "bid_qty": _int(raw.get("bid_qty") if "bid_qty" in raw else raw.get("Bid Qty")),
            "ask_qty": _int(raw.get("ask_qty") if "ask_qty" in raw else raw.get("Ask Qty")),
            "volume": _int(raw.get("volume") if "volume" in raw else raw.get("TTQ")),
            "oi": _int(raw.get("oi") if "oi" in raw else raw.get("OI")),
            "iv": _num(raw.get("iv") if "iv" in raw else raw.get("implied_volatility")),
            "delta": _num(raw.get("delta")),
            "gamma": _num(raw.get("gamma")),
            "theta": _num(raw.get("theta")),
            "vega": _num(raw.get("vega")),
            "sequence": None if seq in (None, "") else int(seq),
            "iv_source": None if raw.get("iv") in (None, "") else "truedata",
            "greek_source": None if all(raw.get(k) in (None, "") for k in ("delta", "gamma", "theta", "vega")) else "truedata",
        }
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
        raw = parts
    if isinstance(raw, (list, tuple)):
        if len(raw) < 3:
            raise GrowConfigError("UNKNOWN_INSTRUMENT")
        padded = list(raw) + [""] * (19 - len(raw))
        first = str(padded[0]).strip()
        symbol_id = first if first.isdigit() else None
        provider_symbol = "" if first.isdigit() else first
        seq = padded[14]
        return {
            "provider_symbol": provider_symbol,
            "symbol_id": symbol_id,
            "timestamp": padded[1],
            "ltp": _num(padded[2]),
            "volume": _int(padded[5]),
            "oi": _int(padded[10]),
            "sequence": None if seq in (None, "") else int(seq),
            "bid": _num(padded[15]),
            "bid_qty": _int(padded[16]),
            "ask": _num(padded[17]),
            "ask_qty": _int(padded[18]),
            "iv": None,
            "delta": None,
            "gamma": None,
            "theta": None,
            "vega": None,
            "iv_source": None,
            "greek_source": None,
        }
    raise GrowConfigError("UNKNOWN_INSTRUMENT")


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise GrowConfigError("INVALID_QUOTE") from exc


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise GrowConfigError("INVALID_QUOTE") from exc
