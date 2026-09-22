"""Map a mock/vendor stream payload onto MarketSnapshot + OptionChainSnapshot."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any, Mapping

from grow.clock import IST
from grow.data.schema import (
    Bar,
    BarSeries,
    MarketSnapshot,
    SnapshotQuality,
    SourceMeta,
    Timeframe,
)
from grow.errors import GrowConfigError
from grow.history.universe import default_index_registry, is_forbidden_instrument
from grow.live_data.models import ADAPTER_VERSION, MOCK_PROVIDER_ID, SCHEMA, LiveSnapshot
from grow.market.session import SessionCalendar
from grow.options.models import ExpiryClass, OptionChainSnapshot, OptionContract, OptionExpiry, OptionType
from grow.types import SessionState, Symbol

STREAM_META = SourceMeta(
    name=MOCK_PROVIDER_ID,
    vendor="grow-mock",
    license="synthetic-stream-not-licensed",
    is_live=True,
    is_fixture=False,
    schema=SCHEMA,
)


def _aware(value: Any, label: str) -> datetime:
    if value is None or value == "":
        raise GrowConfigError(f"MISSING_TIMESTAMP:{label}")
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise GrowConfigError(f"INVALID_TIMESTAMP:{label}") from exc
    if stamp.tzinfo is None or stamp.tzinfo.utcoffset(stamp) is None:
        raise GrowConfigError(f"NAIVE_TIMESTAMP:{label}")
    stamp = stamp.astimezone(IST)
    if getattr(stamp.tzinfo, "key", None) != "Asia/Kolkata":
        raise GrowConfigError(f"TIMEZONE:{label}")
    return stamp


def canonical_contract_id(underlying: str, expiry: str, strike: float, option_type: str) -> str:
    return f"{underlying}-{expiry}-{int(strike)}-{option_type}"


def normalize_event(
    raw: Mapping[str, Any],
    *,
    now: datetime,
    max_staleness_seconds: int,
    calendar: SessionCalendar,
    last_sequence: int | None = None,
) -> LiveSnapshot:
    provider = str(raw.get("provider") or "")
    if raw.get("is_fixture") is True or provider in {"fixture", "historical"}:
        raise GrowConfigError("FIXTURE_FALLBACK_FORBIDDEN")
    if provider != MOCK_PROVIDER_ID:
        raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{provider}")
    if str(raw.get("source_timezone") or "Asia/Kolkata") != "Asia/Kolkata":
        raise GrowConfigError("SOURCE_TIMEZONE")
    if str(raw.get("instrument_type") or "OPTIDX") != "OPTIDX":
        raise GrowConfigError("UNSUPPORTED_INSTRUMENT_TYPE")
    event_time = _aware(raw.get("event_time"), "event_time")
    received_time = _aware(raw.get("received_time") or raw.get("event_time"), "received_time")
    if received_time < event_time:
        raise GrowConfigError("TIMESTAMP_INVERTED")
    try:
        sequence = int(raw.get("sequence") or 0)
    except (TypeError, ValueError) as exc:
        raise GrowConfigError("INVALID_SEQUENCE") from exc
    if sequence < 1:
        raise GrowConfigError("INVALID_SEQUENCE")
    diagnostics: list[str] = []
    if last_sequence is not None and sequence == last_sequence:
        raise GrowConfigError("DUPLICATE_SEQUENCE")
    if last_sequence is not None and sequence < last_sequence:
        raise GrowConfigError("OUT_OF_ORDER")
    age = (now.astimezone(IST) - event_time).total_seconds()
    freshness_ok = age <= max_staleness_seconds
    if not freshness_ok:
        diagnostics.append(f"STALE:{age:.1f}s>{max_staleness_seconds}s")
    underlyings = tuple(str(s).strip().upper() for s in (raw.get("underlyings") or ()))
    if not underlyings:
        raise GrowConfigError("MISSING_UNDERLYINGS")
    registry = default_index_registry()
    for symbol in underlyings:
        if is_forbidden_instrument(symbol) or not registry.allows(symbol, event_time.date()):
            raise GrowConfigError(f"UNSUPPORTED_UNDERLYING:{symbol}")
    bars_by: dict[str, dict[Timeframe, list[Bar]]] = {s: {Timeframe.M15: [], Timeframe.D1: []} for s in underlyings}
    for row in raw.get("spot_bars") or []:
        symbol = str(row["underlying"]).upper()
        if symbol not in bars_by:
            raise GrowConfigError(f"UNSUPPORTED_UNDERLYING:{symbol}")
        tf = Timeframe(str(row["timeframe"]).upper())
        start = _aware(row["start"], "bar.start")
        end = _aware(row["end"], "bar.end")
        bars_by[symbol][tf].append(
            Bar(
                symbol=Symbol(symbol),
                timeframe=tf,
                start=start,
                end=end,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
            )
        )
    spots = {str(k).upper(): float(v) for k, v in (raw.get("spots") or {}).items()}
    contracts_raw = list(raw.get("contract_master") or [])
    quotes_raw = list(raw.get("option_quotes") or [])
    quotes_by: dict[tuple[str, str, float, str], dict[str, Any]] = {}
    for row in quotes_raw:
        key = (
            str(row["underlying"]).upper(),
            str(row["expiry"]),
            float(row["strike"]),
            str(row["option_type"]).upper(),
        )
        quotes_by[key] = row
    chains: dict[str, OptionChainSnapshot] = {}
    lot_sizes: dict[str, int] = {}
    for symbol in underlyings:
        chain, lots = _chain_for(symbol, event_time, spots.get(symbol), contracts_raw, quotes_by, raw)
        chains[symbol] = chain
        lot_sizes.update(lots)
    market: dict[str, MarketSnapshot] = {}
    for symbol in underlyings:
        series: dict[Timeframe, BarSeries] = {}
        for tf, rows in bars_by[symbol].items():
            ordered = tuple(sorted(rows, key=lambda bar: bar.start))
            series[tf] = BarSeries(symbol=Symbol(symbol), timeframe=tf, bars=ordered)
        m15 = series.get(Timeframe.M15)
        last = spots.get(symbol)
        if last is None and m15 is not None and m15.bars:
            last = m15.bars[-1].close
        if last is None:
            raise GrowConfigError(f"MISSING_SPOT:{symbol}")
        last_end = m15.bars[-1].end if m15 is not None and m15.bars else event_time
        snap_id = str(raw.get("snapshot_id") or _digest(f"{symbol}:{sequence}:{event_time.isoformat()}"))
        if len(underlyings) > 1:
            snap_id = f"{snap_id}:{symbol}"
        market[symbol] = MarketSnapshot(
            snapshot_id=snap_id,
            symbol=Symbol(symbol),
            as_of=event_time,
            session=_session(calendar, event_time),
            last_price=last,
            currency="INR",
            series=series,
            quality=SnapshotQuality(
                complete=bool(m15 and m15.bars),
                stale=not freshness_ok,
                missing_count=0 if m15 and m15.bars else 1,
                expected_count=1,
                last_bar_end=last_end,
                notes=tuple(diagnostics),
            ),
            source=STREAM_META,
        )
    snapshot_id = str(raw.get("snapshot_id") or _digest(f"{sequence}:{event_time.isoformat()}"))
    return LiveSnapshot(
        snapshot_id=snapshot_id,
        schema=SCHEMA,
        provider_id=MOCK_PROVIDER_ID,
        adapter_version=str(raw.get("adapter_version") or ADAPTER_VERSION),
        sequence=sequence,
        event_time=event_time,
        received_time=received_time,
        session_date=date.fromisoformat(str(raw.get("session_date") or event_time.date())),
        underlyings=underlyings,
        market=market,
        chains=chains,
        lot_sizes=lot_sizes,
        freshness_ok=freshness_ok,
        diagnostics=tuple(diagnostics),
    )


def _session(calendar: SessionCalendar, as_of: datetime) -> SessionState:
    return calendar.state(as_of)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _chain_for(
    symbol: str,
    as_of: datetime,
    spot: float | None,
    master: list[dict[str, Any]],
    quotes: dict[tuple[str, str, float, str], dict[str, Any]],
    raw: Mapping[str, Any],
) -> tuple[OptionChainSnapshot, dict[str, int]]:
    rows = [row for row in master if str(row.get("underlying")).upper() == symbol]
    contracts: list[OptionContract] = []
    expiries: dict[date, ExpiryClass] = {}
    lots: dict[str, int] = {}
    for row in rows:
        if str(row.get("instrument_type") or "OPTIDX") != "OPTIDX":
            raise GrowConfigError(f"UNSUPPORTED_INSTRUMENT_TYPE:{row.get('instrument_type')}")
        kind = str(row.get("option_type") or "").upper()
        if kind not in {"CE", "PE"}:
            raise GrowConfigError(f"INVALID_OPTION_TYPE:{kind}")
        try:
            expiry = date.fromisoformat(str(row["expiry"]))
        except (TypeError, ValueError, KeyError) as exc:
            raise GrowConfigError("INVALID_EXPIRY") from exc
        try:
            strike = float(row["strike"])
        except (TypeError, ValueError, KeyError) as exc:
            raise GrowConfigError("INVALID_STRIKE") from exc
        if strike <= 0:
            raise GrowConfigError("INVALID_STRIKE")
        provider_id = str(row.get("tradingsymbol") or row.get("provider_contract_id") or "")
        cid = canonical_contract_id(symbol, expiry.isoformat(), strike, kind)
        if not provider_id:
            raise GrowConfigError("MISSING_PROVIDER_CONTRACT_ID")
        if provider_id == cid:
            raise GrowConfigError("IDENTITY_AMBIGUITY")
        lot = row.get("lot_size")
        if lot is not None:
            try:
                lots[cid] = int(lot)
                lots[provider_id] = int(lot)
            except (TypeError, ValueError) as exc:
                raise GrowConfigError("INVALID_LOT_SIZE") from exc
        klass = ExpiryClass.MONTHLY if str(row.get("expiry_class") or "WEEKLY").upper() == "MONTHLY" else ExpiryClass.WEEKLY
        expiries[expiry] = klass
        quote = quotes.get((symbol, expiry.isoformat(), strike, kind), {})
        ts = _aware(quote.get("ts") or as_of, "quote.ts") if quote else as_of
        bid = None if quote.get("bid") is None else float(quote["bid"])
        ask = None if quote.get("ask") is None else float(quote["ask"])
        ltp = None if quote.get("ltp") is None else float(quote["ltp"])
        contracts.append(
            OptionContract(
                underlying=symbol,
                expiry=expiry,
                expiry_class=klass,
                strike=strike,
                option_type=OptionType.CE if kind == "CE" else OptionType.PE,
                bid=bid,
                ask=ask,
                last_price=ltp,
                volume=int(quote.get("volume") or 0),
                open_interest=int(quote.get("oi") or 0),
                previous_open_interest=None,
                implied_volatility=None,
                delta=None,
                gamma=None,
                theta=None,
                vega=None,
                timestamp=ts,
                provider_contract_id=provider_id,
            )
        )
    if spot is None:
        raise GrowConfigError(f"MISSING_SPOT:{symbol}")
    chain_id = _digest(f"chain:{symbol}:{as_of.isoformat()}:{raw.get('sequence')}")
    chain = OptionChainSnapshot(
        snapshot_id=chain_id,
        underlying=symbol,
        as_of=as_of,
        spot=spot,
        expiries=tuple(OptionExpiry(day, klass) for day, klass in sorted(expiries.items())),
        contracts=tuple(contracts),
        source_id=MOCK_PROVIDER_ID,
        is_fixture=False,
        provider_metadata={"provider": MOCK_PROVIDER_ID, "schema": SCHEMA},
    )
    return chain, lots
