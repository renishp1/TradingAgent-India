"""Build a multi-index LIVE AgentMarketSnapshot for campaign paper runtime.

NIFTY (NSE/NFO) and SENSEX (BSE/BFO) are fetched independently. One index
succeeding never suppresses the other. History is stored per underlying.
"""

from __future__ import annotations

import importlib
import json
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence
from uuid import uuid4

from grow.clock import IST, Clock
from grow.errors import GrowConfigError
from grow.live_data.expiry_class import ExpiryClassifier
from grow.live_data.kite_market import (
    BFO_UNDERLYINGS,
    CAMPAIGN_UNDERLYINGS,
    HISTORY_KITE_INTERVAL,
    HISTORY_LOOKBACK_CALENDAR_DAYS,
    HISTORY_MIN_CLOSES,
    HISTORY_TARGET_BARS,
    HISTORY_TIMEFRAME,
    INDEX_EXCHANGE,
    INDEX_QUERY,
    INDEX_TOKEN,
    NFO_UNDERLYINGS,
    OPTION_QUOTE_PREFIX,
    QUOTE_URL,
    RealKiteTransport,
    candles_to_m15_spot_bars,
    decode_http_text,
    parse_bfo_instruments,
    parse_nfo_instruments,
)
from grow.market_data.normalized.models import (
    SCHEMA,
    AgentMarketSnapshot,
    DataQualityStatus,
    OptionQuoteView,
    UnderlyingQuoteView,
    snapshot_digest,
)
from grow.market_data.provenance import MarketDataSource
from grow.market_data.snapshots.builder import history_diagnostics_from_closes

STRIKE_WINDOW = 5


def build_campaign_live_snapshot(
    transport: RealKiteTransport,
    clock: Clock,
    *,
    underlyings: Sequence[str] = CAMPAIGN_UNDERLYINGS,
) -> tuple[AgentMarketSnapshot, dict[str, Any]]:
    """LIVE multi-index snapshot. Failures for one index do not drop others."""
    now = clock.now().astimezone(IST)
    wanted = tuple(dict.fromkeys(str(u).strip().upper() for u in underlyings if str(u).strip()))
    if not wanted:
        raise GrowConfigError("EMPTY_CAMPAIGN_UNIVERSE")

    nfo_catalog = parse_nfo_instruments(transport.fetch_instruments()) if any(u in NFO_UNDERLYINGS for u in wanted) else ()
    bfo_catalog = parse_bfo_instruments(transport.fetch_bfo_instruments()) if any(u in BFO_UNDERLYINGS for u in wanted) else ()
    classifier = ExpiryClassifier(clock=clock)

    under_views: dict[str, UnderlyingQuoteView] = {}
    options: list[OptionQuoteView] = []
    history_by: dict[str, list[float]] = {}
    per_index: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}

    for name in wanted:
        try:
            meta = _build_one_index(
                transport,
                clock,
                classifier=classifier,
                underlying=name,
                catalog=nfo_catalog if name in NFO_UNDERLYINGS else bfo_catalog,
                now=now,
            )
        except GrowConfigError as exc:
            errors[name] = str(exc)
            continue
        under_views[name] = meta["underlying"]
        options.extend(meta["options"])
        history_by[name] = list(meta["history_closes"])
        per_index[name] = {
            "spot": meta["spot"],
            "expiry": meta["expiry"],
            "history_bar_count": len(meta["history_closes"]),
            "history_interval": HISTORY_TIMEFRAME,
            "history_provenance": "LIVE",
            "option_count": len(meta["options"]),
            "provenance": "LIVE",
            "lot_sizes": sorted({int(q.lot_size) for q in meta["options"] if q.lot_size}),
        }

    if not under_views:
        raise GrowConfigError("NO_VALID_INDEX:" + ",".join(f"{k}={v}" for k, v in errors.items()))

    # Backward-compatible primary history: first configured index that has bars.
    primary = next(iter(history_by))
    primary_hist = history_diagnostics_from_closes(
        history_by[primary],
        interval=HISTORY_TIMEFRAME,
        earliest=None,
        latest=None,
        provenance="LIVE",
    )
    diagnostics: dict[str, Any] = {
        **primary_hist,
        "history_closes_by_underlying": {k: list(v) for k, v in history_by.items()},
        "history_interval": HISTORY_TIMEFRAME,
        "history_provenance": "LIVE",
        "market_data_source": "LIVE",
        "market_data_health": "HEALTHY",
        "freshness_ok": True,
        "fixture": False,
        "quote_path": "HTTP_REST",
        "campaign_underlyings": list(under_views.keys()),
        "per_index": per_index,
        "index_errors": errors,
    }

    snap_id = f"live-campaign-{now.strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
    version = snapshot_digest(
        {
            "as_of": now.isoformat(),
            "underlyings": sorted(under_views),
            "options": len(options),
            "history": {k: len(v) for k, v in history_by.items()},
        }
    )
    snapshot = AgentMarketSnapshot(
        snapshot_id=snap_id,
        version=version,
        schema=SCHEMA,
        provider="kite_market",
        exchange="MULTI",
        session_timestamp=now,
        decision_timestamp=now,
        session_date=now.date(),
        underlyings=under_views,
        option_contracts=tuple(options),
        data_quality=DataQualityStatus.OK,
        quality_notes=(),
        source_snapshot_ids={"kite": snap_id},
        diagnostics=diagnostics,
        paper_mode=True,
        live_trading=False,
        market_data_source=MarketDataSource.LIVE,
    )
    summary = {
        "provenance": "LIVE",
        "underlyings": list(under_views.keys()),
        "per_index": per_index,
        "index_errors": errors,
        "history_bar_count": primary_hist.get("history_bar_count"),
        "history_interval": HISTORY_TIMEFRAME,
        "history_provenance": "LIVE",
    }
    return snapshot, summary


def _build_one_index(
    transport: RealKiteTransport,
    clock: Clock,
    *,
    classifier: ExpiryClassifier,
    underlying: str,
    catalog: Sequence[Any],
    now: datetime,
) -> dict[str, Any]:
    name = underlying.upper()
    if name not in INDEX_QUERY:
        raise GrowConfigError(f"UNSUPPORTED_INDEX:{name}")
    spot, token = transport.fetch_index_quote(name)
    spot = float(spot)
    query = INDEX_QUERY[name]
    parse = importlib.import_module("urllib.parse")
    body = json.loads(decode_http_text(transport._get(f"{QUOTE_URL}?i={parse.quote(query)}")))
    row = next(iter((body.get("data") or {}).values()), {}) or {}
    ohlc = row.get("ohlc") or {}
    under = UnderlyingQuoteView(
        underlying=name,
        exchange=INDEX_EXCHANGE.get(name, "NSE"),
        spot=spot,
        ltp=spot,
        open=float(ohlc["open"]) if ohlc.get("open") is not None else None,
        high=float(ohlc["high"]) if ohlc.get("high") is not None else None,
        low=float(ohlc["low"]) if ohlc.get("low") is not None else None,
        close=float(ohlc["close"]) if ohlc.get("close") is not None else None,
        volume=None,
        quote_timestamp=now,
        quote_age_seconds=0.0,
    )
    pool_all = [r for r in catalog if r.underlying == name]
    if not pool_all:
        raise GrowConfigError(f"NO_OPTIONS:{name}")
    call, put = _pair_for_underlying(pool_all, spot=spot, as_of=now.date(), classifier=classifier, underlying=name)
    expiry = call.expiry
    pool = [r for r in pool_all if r.expiry == expiry]
    strikes = sorted({float(r.strike) for r in pool})
    atm = float(call.strike)
    window = sorted(sorted(strikes, key=lambda s: (abs(s - atm), s))[: STRIKE_WINDOW * 2 + 1])
    wanted: list[Any] = []
    for strike in window:
        for side in ("CE", "PE"):
            match = next((r for r in pool if float(r.strike) == strike and r.option_type == side), None)
            if match is not None:
                wanted.append(match)
    if not wanted:
        raise GrowConfigError(f"NO_VALID_OPTION:{name}")

    prefix = OPTION_QUOTE_PREFIX[name]
    parse = importlib.import_module("urllib.parse")
    parts = [f"i={parse.quote(prefix + ':' + r.tradingsymbol)}" for r in wanted]
    qbody = json.loads(decode_http_text(transport._get(f"{QUOTE_URL}?{'&'.join(parts)}")))
    data = qbody.get("data") or {}
    if not isinstance(data, dict) or not data:
        raise GrowConfigError(f"METADATA_UNAVAILABLE:{name}")
    by_sym = {r.tradingsymbol: r for r in wanted}
    options: list[OptionQuoteView] = []
    for key, qrow in data.items():
        if not isinstance(qrow, dict):
            continue
        sym = str(key).split(":", 1)[-1]
        meta = by_sym.get(sym)
        if meta is None:
            continue
        depth = qrow.get("depth") or {}
        buy = depth.get("buy") or [{}]
        sell = depth.get("sell") or [{}]
        bid = (buy[0] or {}).get("price")
        ask = (sell[0] or {}).get("price")
        ltp = qrow.get("last_price")
        classified = classifier.classify(
            provider_symbol=meta.tradingsymbol,
            canonical_symbol=name,
            expiry=meta.expiry,
            option_type=meta.option_type,
            as_of=now,
        )
        if not str(classified.expiry_class or "").upper() in {"WEEKLY", "MONTHLY"}:
            continue
        options.append(
            OptionQuoteView(
                underlying=name,
                expiry=meta.expiry,
                strike=float(meta.strike),
                option_type=meta.option_type,
                ltp=None if ltp is None else float(ltp),
                bid=None if bid is None else float(bid),
                ask=None if ask is None else float(ask),
                open_interest=qrow.get("oi"),
                volume=qrow.get("volume"),
                quote_timestamp=now,
                quote_age_seconds=0.0,
                provider_contract_id=meta.tradingsymbol,
                quality=DataQualityStatus.OK,
                lot_size=int(meta.lot_size or 0) or None,
                expiry_class=classified.expiry_class,
                is_fixture=False,
            )
        )
    if not options:
        raise GrowConfigError(f"NO_VALID_OPTION:{name}")
    if any(row.lot_size is None or int(row.lot_size) < 1 for row in options):
        raise GrowConfigError(f"MISSING_LOT_SIZE:{name}")

    hist_token = int(token) if token else INDEX_TOKEN.get(name)
    if hist_token is None:
        raise GrowConfigError(f"MISSING_INDEX_TOKEN:{name}")
    to_day = now.date()
    from_day = to_day - timedelta(days=HISTORY_LOOKBACK_CALENDAR_DAYS)
    candles = transport.fetch_historical_candles(
        hist_token,
        interval=HISTORY_KITE_INTERVAL,
        from_date=from_day,
        to_date=to_day,
        as_of=now,
    )
    bars = candles_to_m15_spot_bars(candles, underlying=name)
    if len(bars) > HISTORY_TARGET_BARS:
        bars = bars[-HISTORY_TARGET_BARS:]
    closes = [float(row["close"]) for row in bars]
    if len(closes) < HISTORY_MIN_CLOSES:
        raise GrowConfigError(f"INSUFFICIENT_M15:{name}:{len(closes)}")

    return {
        "underlying": under,
        "options": options,
        "history_closes": closes,
        "spot": spot,
        "expiry": expiry.isoformat(),
    }


def _pair_for_underlying(options, *, spot: float, as_of: date, classifier: ExpiryClassifier, underlying: str):
    from grow.live_data.kite_market import _classified_live_rows, _preferred_live_expiry

    grouped = _classified_live_rows(options, as_of=as_of, classifier=classifier)
    rows = grouped.get(underlying) or []
    if not rows:
        # classify into a temp map for this underlying only
        rows = []
        for row in options:
            if row.underlying != underlying or row.expiry < as_of:
                continue
            classified = classifier.classify(
                provider_symbol=row.tradingsymbol,
                canonical_symbol=underlying,
                expiry=row.expiry,
                option_type=row.option_type,
                as_of=as_of,
            )
            klass = str(classified.expiry_class or "").upper()
            if klass in {"WEEKLY", "MONTHLY"}:
                rows.append((row, klass))
    if not rows:
        raise GrowConfigError(f"NO_VALID_OPTION:{underlying}")
    expiry = _preferred_live_expiry(rows, underlying=underlying, as_of=as_of)
    if expiry is None:
        raise GrowConfigError(f"NO_VALID_EXPIRY:{underlying}")
    pool = [row for row, _k in rows if row.expiry == expiry]
    strikes = sorted({row.strike for row in pool}, key=lambda value: (abs(value - float(spot)), value))
    for strike in strikes:
        at_strike = [row for row in pool if row.strike == strike]
        calls = [row for row in at_strike if row.option_type == "CE"]
        puts = [row for row in at_strike if row.option_type == "PE"]
        if calls and puts:
            return calls[0], puts[0]
    raise GrowConfigError(f"NO_VALID_OPTION:{underlying}")


__all__ = ["build_campaign_live_snapshot", "CAMPAIGN_UNDERLYINGS"]
