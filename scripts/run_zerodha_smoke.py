#!/usr/bin/env python3
"""Credential-gated Zerodha 3C.3 smoke. Paper only. Disabled unless ZERODHA_SMOKE=1.

Usage (never commit secrets):

    export ZERODHA_SMOKE=1
    export KITE_API_KEY=...
    export KITE_ACCESS_TOKEN=...
    export GROW_RISK_SECRET=...
    python scripts/run_zerodha_smoke.py

The access token is read here and passed only to the market-data adapter.
It is removed before the paper boot lock runs. This command does not place,
modify, or cancel orders, and it does not inject ticks.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grow.clock import SystemClock  # noqa: E402
from grow.errors import GrowConfigError, GrowLiveTradingDisabled  # noqa: E402
from grow.execution.lock import LIVE_TRADING_COMPILED  # noqa: E402
from grow.live_data.kite_market import KiteMarketProvider, load_kite_market_secrets  # noqa: E402
from grow.live_data.normalize import normalize_event  # noqa: E402
from grow.live_data.smoke import (  # noqa: E402
    assess_option_ticks,
    kite_market_smoke_config,
    redact_text,
    smoke_risk_secret,
    write_smoke_report,
)
from grow.market.session import SessionCalendar  # noqa: E402


def _report_path(session_id: str) -> Path:
    return ROOT / "results" / f"zerodha-smoke-{session_id}.json"


def _public_error(exc: BaseException, secrets: tuple[str, ...]) -> str:
    if isinstance(exc, GrowConfigError):
        message = str(exc)
    elif isinstance(exc, GrowLiveTradingDisabled):
        message = str(exc)
    else:
        message = "NETWORK_ERROR"
    redacted = redact_text(message, secrets)
    lowered = redacted.lower()
    if "wss://" in lowered or "api_key=" in lowered or "access_token=" in lowered or "authorization" in lowered:
        return "CONNECT_FAILED"
    return redacted


def _side_book(snapshot: Any, quote: dict[str, Any]) -> tuple[Any, Any]:
    if snapshot is None:
        return None, None
    symbol = str(quote.get("provider_symbol") or "")
    for chain in (getattr(snapshot, "chains", {}) or {}).values():
        for contract in getattr(chain, "contracts", ()) or ():
            if str(getattr(contract, "provider_contract_id", "") or "") != symbol:
                continue
            return getattr(contract, "volume", None), getattr(contract, "open_interest", None)
    return None, None


def _mark(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _format_proof(proof: dict[str, Any]) -> str:
    lines = [
        "## Zerodha 3C.3 Smoke",
        "",
        "Provider: Zerodha/Kite",
        "Underlying: NIFTY",
        f"Session: {proof['session']}",
        "",
        f"Catalog: {_mark(proof['catalog'])}",
        f"CE contract: {_mark(proof['ce_contract'])}",
        f"PE contract: {_mark(proof['pe_contract'])}",
        f"WebSocket: {_mark(proof['websocket'])}",
        f"CE tick: {_mark(proof['ce_tick'])}",
        f"PE tick: {_mark(proof['pe_tick'])}",
        f"CE timestamp: {_mark(proof['ce_timestamp'])}",
        f"PE timestamp: {_mark(proof['pe_timestamp'])}",
        f"CE freshness: {_mark(proof['ce_freshness'])}",
        f"PE freshness: {_mark(proof['pe_freshness'])}",
        f"Snapshot normalization: {_mark(proof['snapshot'])}",
        f"Paper-only guard: {_mark(proof['paper_only'])}",
        "Order route invoked: NO",
        "",
        "Overall:",
        f"3C.3 REAL-DATA = {proof['result']}",
    ]
    for label, quote in (("CE", proof.get("ce")), ("PE", proof.get("pe"))):
        if not quote:
            continue
        lines.extend(
            [
                "",
                f"{label} evidence:",
                f"  underlying: {quote.get('underlying')}",
                f"  expiry: {quote.get('expiry')}",
                f"  strike: {quote.get('strike')}",
                f"  option_type: {quote.get('option_type')}",
                f"  instrument_token: {quote.get('provider_symbol_id')}",
                f"  trading_symbol: {quote.get('provider_symbol')}",
                f"  canonical_id: {quote.get('canonical_id')}",
                f"  ltp: {quote.get('ltp')}",
                f"  bid: {quote.get('bid')}",
                f"  ask: {quote.get('ask')}",
                f"  volume: {quote.get('volume')}",
                f"  open_interest: {quote.get('open_interest')}",
                f"  exchange_timestamp: {quote.get('quote_timestamp')}",
                f"  received_timestamp: {quote.get('received_time')}",
                f"  quote_age_seconds: {quote.get('quote_age_seconds')}",
                f"  freshness: {_mark(quote.get('quote_freshness') is True)}",
            ]
        )
    if proof.get("error"):
        lines.extend(["", f"Error: {proof['error']}"])
    return "\n".join(lines)


def _blank_proof(session: str) -> dict[str, Any]:
    return {
        "session": session,
        "catalog": False,
        "ce_contract": False,
        "pe_contract": False,
        "websocket": False,
        "ce_tick": False,
        "pe_tick": False,
        "ce_timestamp": False,
        "pe_timestamp": False,
        "ce_freshness": False,
        "pe_freshness": False,
        "snapshot": False,
        "paper_only": False,
        "ce": None,
        "pe": None,
        "error": None,
        "result": "NOT PROVEN",
    }


def _finish(proof: dict[str, Any], secrets: tuple[str, ...]) -> int:
    if proof.get("error"):
        proof["error"] = redact_text(str(proof["error"]), secrets)
    passed = (
        proof["catalog"]
        and proof["ce_contract"]
        and proof["pe_contract"]
        and proof["websocket"]
        and proof["ce_tick"]
        and proof["pe_tick"]
        and proof["ce_timestamp"]
        and proof["pe_timestamp"]
        and proof["ce_freshness"]
        and proof["pe_freshness"]
        and proof["snapshot"]
        and proof["paper_only"]
        and proof.get("ce")
        and proof.get("pe")
        and proof["ce"].get("underlying") == "NIFTY"
        and proof["pe"].get("underlying") == "NIFTY"
        and proof["ce"].get("quote_freshness") is True
        and proof["pe"].get("quote_freshness") is True
    )
    proof["result"] = "PASS" if passed else "NOT PROVEN"
    text = _format_proof(proof)
    print(redact_text(text, secrets))
    safe = json.loads(redact_text(json.dumps(proof, default=str), secrets))
    path = write_smoke_report(safe, _report_path(proof["session"].replace(":", "").replace("+", "")))
    print(f"smoke_report={path}", file=sys.stderr)
    return 0 if passed else 2


def _remember(proof: dict[str, Any], quote: dict[str, Any], volume: Any, oi: Any) -> None:
    row = dict(quote)
    row["volume"] = volume
    row["open_interest"] = oi
    side = str(row.get("option_type") or "")
    if side == "CE":
        proof["ce"] = row
        proof["ce_tick"] = True
        proof["ce_timestamp"] = bool(row.get("quote_timestamp"))
        proof["ce_freshness"] = row.get("quote_freshness") is True
    elif side == "PE":
        proof["pe"] = row
        proof["pe_tick"] = True
        proof["pe_timestamp"] = bool(row.get("quote_timestamp"))
        proof["pe_freshness"] = row.get("quote_freshness") is True


def main(environ: dict[str, str] | None = None) -> int:
    env = dict(os.environ if environ is None else environ)
    secrets: tuple[str, ...] = ()
    clock = SystemClock()
    proof = _blank_proof(clock.now().isoformat())
    if LIVE_TRADING_COMPILED:
        proof["error"] = "LIVE_TRADING_COMPILED"
        print(proof["error"], file=sys.stderr)
        return 1
    provider = None
    try:
        config = kite_market_smoke_config(env)
        proof["paper_only"] = bool(config.live_data.paper_mode) and not config.live_data.live_trading and not config.execution.live_trading_enabled
        api_key, access_token = load_kite_market_secrets(env)
        risk_secret = smoke_risk_secret({key: value for key, value in env.items() if key not in {"KITE_API_KEY", "KITE_ACCESS_TOKEN"}})
        secrets = tuple(item for item in (api_key, access_token, risk_secret) if item)
        provider = KiteMarketProvider(api_key=api_key, access_token=access_token, clock=clock)
        provider.connect()
        call = provider.selected
        put = provider.selected_put
        nifty_options = [row for row in provider._options if row.underlying == "NIFTY"]
        proof["catalog"] = bool(nifty_options)
        proof["ce_contract"] = call is not None and call.underlying == "NIFTY" and call.option_type == "CE"
        proof["pe_contract"] = put is not None and put.underlying == "NIFTY" and put.option_type == "PE"
        transport = provider.transport
        proof["websocket"] = bool(
            transport is not None
            and getattr(transport, "connected", False)
            and getattr(transport, "mode", None) == "full"
            and call is not None
            and put is not None
            and call.instrument_token in getattr(transport, "subscribed", ())
            and put.instrument_token in getattr(transport, "subscribed", ())
        )
        if not proof["paper_only"]:
            proof["error"] = "LIVE_EXECUTION_FORBIDDEN"
            return _finish(proof, secrets)
        deadline = time.monotonic() + float(config.live_data.session_timeout_seconds)
        calendar = SessionCalendar(config.market, clock=clock)
        while time.monotonic() < deadline and not (proof["ce_freshness"] and proof["pe_freshness"]):
            raw = provider.poll()
            if not isinstance(raw, dict) or raw.get("kind") in {"control", "heartbeat"} or "option_quotes" not in raw:
                continue
            if not raw.get("option_quotes"):
                continue
            try:
                snapshot = normalize_event(
                    raw,
                    now=clock.now(),
                    max_staleness_seconds=config.live_data.max_staleness_seconds,
                    calendar=calendar,
                )
            except GrowConfigError:
                continue
            proof["snapshot"] = True
            check = assess_option_ticks(
                snapshot,
                provider._symbol_ids,
                max_staleness_seconds=config.live_data.max_staleness_seconds,
                now=clock.now(),
            )
            for quote in check.quotes:
                volume, oi = _side_book(snapshot, quote)
                _remember(proof, dict(quote), volume, oi)
        return _finish(proof, secrets)
    except (GrowConfigError, GrowLiveTradingDisabled) as exc:
        proof["error"] = _public_error(exc, secrets)
        print(proof["error"], file=sys.stderr)
        if "LIVE_" in proof["error"] or "BROKER" in proof["error"]:
            return 1
        return _finish(proof, secrets)
    except Exception as exc:
        proof["error"] = _public_error(exc, secrets)
        print(proof["error"], file=sys.stderr)
        return _finish(proof, secrets)
    finally:
        if provider is not None:
            try:
                provider.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
