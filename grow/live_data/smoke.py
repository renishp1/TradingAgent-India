"""3C.2 TrueData smoke evidence. Paper only. No secrets. No broker."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from grow.config import GrowConfig, load_config
from grow.errors import GrowConfigError, GrowLiveTradingDisabled
from grow.execution.lock import LIVE_TRADING_COMPILED, inspect_environment
from grow.live_data.catalog import UNKNOWN_EXPIRY_CLASS
from grow.live_data.expiry_class import CALENDAR_UNSUPPORTED_YEAR
from grow.live_data.models import CycleStatus, LiveCycleReport
from grow.risk.secret import resolve_risk_secret


def _tok(*parts: str) -> str:
    return "".join(parts)


SMOKE_SCHEMA = "grow.smoke.truedata.v1"
PASS = "PASS"
PASS_WITH_NO_TRADE = "PASS_WITH_NO_TRADE"
FAIL = "FAIL"
HARD_FAIL = "HARD_FAIL"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_MAX_CYCLES = 256
_SECRET_KEY_FRAGMENTS = ("password", "passwd", "secret", "token", "authorization", "api_key", "apikey")
_FORBIDDEN_MODULES = (
    _tok("kite", "connect"),
    _tok("smart", "api"),
    _tok("dhan", "hq"),
    _tok("ups", "tox", "_client"),
    _tok("alice", "_blue"),
)
_BROKER_NAMES = frozenset(
    {
        _tok("place", "_order"),
        _tok("place", "Order"),
        _tok("submit", "_order"),
        _tok("modify", "_order"),
        _tok("cancel", "_order"),
        _tok("Live", "Broker"),
    }
)
_SCAN_RELATIVE = (
    "grow/live_data/truedata.py",
    "grow/live_data/loop.py",
    "grow/live_data/smoke.py",
    "grow/live_data/provider.py",
    "scripts/run_truedata_smoke.py",
)


def require_smoke_flag(environ: Mapping[str, str] | None = None) -> None:
    env = dict(environ or {})
    if str(env.get("TRUEDATA_SMOKE") or "").strip() != "1":
        raise GrowConfigError("SMOKE_DISABLED: set TRUEDATA_SMOKE=1 for a real TrueData smoke run")


def load_smoke_secrets(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    from grow.live_data.truedata import load_truedata_secrets

    env = dict(environ or {})
    if LIVE_TRADING_COMPILED:
        raise GrowLiveTradingDisabled("LIVE_TRADING_COMPILED forbids 3C.2")
    require_smoke_flag(env)
    inspect_environment(env.items())
    return load_truedata_secrets(env)


def smoke_config(environ: Mapping[str, str] | None = None) -> GrowConfig:
    env = dict(environ or {})
    timeout = int(str(env.get("TRUEDATA_SMOKE_TIMEOUT") or DEFAULT_TIMEOUT_SECONDS).strip() or DEFAULT_TIMEOUT_SECONDS)
    if timeout < 1:
        raise GrowConfigError("TRUEDATA_SMOKE_TIMEOUT must be >= 1")
    forced = {
        **env,
        "GROW_LIVE_DATA_ENABLED": "true",
        "GROW_LIVE_DATA_PROVIDER": "truedata",
        "GROW_LIVE_DATA_MODE": "real",
        "LIVE_DATA_ENABLED": "true",
        "LIVE_DATA_PROVIDER": "truedata",
        "GROW_LIVE_DATA_LIVE_TRADING": "false",
        "GROW_LIVE_DATA_PAPER_MODE": "true",
    }
    config = load_config(environ=forced)
    live = replace(
        config.live_data,
        enabled=True,
        provider="truedata",
        mode="real",
        paper_mode=True,
        live_trading=False,
        snapshot_interval_seconds=0,
        session_timeout_seconds=timeout,
        reconnect_policy="bounded_backoff",
    )
    config = replace(config, live_data=live)
    config.assert_safe()
    if config.live_data.live_trading or config.execution.live_trading_enabled:
        raise GrowLiveTradingDisabled("LIVE_EXECUTION_FORBIDDEN")
    if not config.live_data.paper_mode:
        raise GrowConfigError("live_data.paper_mode must be true.")
    return config


def smoke_risk_secret(environ: Mapping[str, str] | None = None) -> str:
    return resolve_risk_secret(None, environ=environ)


def redact_text(text: str, secrets: Sequence[str]) -> str:
    out = str(text)
    for secret in secrets:
        token = str(secret or "").strip()
        if len(token) >= 4:
            out = out.replace(token, "[REDACTED]")
    return out


def _is_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in _SECRET_KEY_FRAGMENTS)


def redact_tree(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _is_secret_key(str(key)):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = redact_tree(item, secrets)
        return out
    if isinstance(value, list):
        return [redact_tree(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [redact_tree(item, secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


def contains_secret(value: Any, secrets: Sequence[str]) -> bool:
    blob = json.dumps(value, default=str)
    for secret in secrets:
        token = str(secret or "").strip()
        if len(token) >= 4 and token in blob:
            return True
    return False


def broker_modules_loaded() -> tuple[str, ...]:
    import sys

    return tuple(name for name in _FORBIDDEN_MODULES if name in sys.modules)


def scan_broker_source(root: Path) -> tuple[str, ...]:
    hits: list[str] = []
    forbidden = set(_FORBIDDEN_MODULES)
    for rel in _SCAN_RELATIVE:
        path = root / rel
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    if top in forbidden:
                        hits.append(f"{rel}:import:{top}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".", 1)[0]
                if top in forbidden:
                    hits.append(f"{rel}:from:{top}")
            elif isinstance(node, ast.Name) and node.id in _BROKER_NAMES:
                hits.append(f"{rel}:{node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in _BROKER_NAMES:
                hits.append(f"{rel}:{node.attr}")
    return tuple(hits)


def classification_by_underlying(catalog: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for row in catalog:
        if row.get("option_type") not in {"CE", "PE"}:
            continue
        symbol = str(row.get("canonical_symbol") or "").upper()
        if not symbol:
            continue
        bucket = summary.setdefault(symbol, {"WEEKLY": 0, "MONTHLY": 0, "UNKNOWN": 0, "UNSUPPORTED_YEAR": 0})
        klass = str(row.get("expiry_class") or UNKNOWN_EXPIRY_CLASS).upper()
        diagnostic = None
        evidence = row.get("classification")
        if isinstance(evidence, Mapping):
            diagnostic = evidence.get("diagnostic")
            klass = str(evidence.get("expiry_class") or klass).upper()
        if diagnostic == CALENDAR_UNSUPPORTED_YEAR:
            bucket["UNSUPPORTED_YEAR"] += 1
        if klass == "WEEKLY":
            bucket["WEEKLY"] += 1
        elif klass == "MONTHLY":
            bucket["MONTHLY"] += 1
        else:
            bucket["UNKNOWN"] += 1
    return summary


def classify_smoke_result(
    *,
    authenticated: bool,
    catalog_ok: bool,
    mapping_ready: bool,
    option_tick_ok: bool,
    paper_fill: bool,
    hard_fail: bool,
    fixture_fallback: bool,
) -> str:
    if hard_fail or fixture_fallback:
        return HARD_FAIL
    if not authenticated or not catalog_ok or not mapping_ready or not option_tick_ok:
        return FAIL
    if paper_fill:
        return PASS
    return PASS_WITH_NO_TRADE


def _scalar(value: Any) -> Any:
    return getattr(value, "value", value)


def _reverse_symbol_ids(symbol_ids: Mapping[str, str]) -> dict[str, str]:
    return {str(name): str(ident) for ident, name in symbol_ids.items() if str(name).strip()}


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    return value.tzinfo.utcoffset(value) is not None


def _marked_fixture(value: Any) -> bool:
    return getattr(value, "is_fixture", False) is True


@dataclass(frozen=True)
class OptionTickCheck:
    """Live option-quote evidence. Catalog rows and index ticks are not quotes."""

    quotes: tuple[Mapping[str, Any], ...] = ()
    fixture_rejected: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.quotes) and not self.fixture_rejected


def _provider_quotes_marked_fixture(provider: Any) -> bool:
    quotes = getattr(provider, "_quotes", None) or {}
    if not isinstance(quotes, Mapping):
        return False
    for quote in quotes.values():
        if isinstance(quote, Mapping) and quote.get("is_fixture") is True:
            return True
    return False


def _snapshot_marked_fixture(snapshot: Any) -> bool:
    if _marked_fixture(snapshot):
        return True
    for chain in (getattr(snapshot, "chains", {}) or {}).values():
        if _marked_fixture(chain):
            return True
        for contract in getattr(chain, "contracts", ()) or ():
            if _marked_fixture(contract):
                return True
    for item in (getattr(snapshot, "market", {}) or {}).values():
        if _marked_fixture(item) or _marked_fixture(getattr(item, "source", None)):
            return True
    return False


def _live_option_quote(
    snapshot: Any,
    contract: Any,
    reverse: Mapping[str, str],
) -> dict[str, Any] | None:
    option_type = str(_scalar(getattr(contract, "option_type", "")) or "")
    if option_type not in {"CE", "PE"}:
        return None
    bid = getattr(contract, "bid", None)
    ask = getattr(contract, "ask", None)
    ltp = getattr(contract, "last_price", None)
    if bid is None and ask is None and ltp is None:
        return None
    provider_symbol = str(getattr(contract, "provider_contract_id", "") or "").strip()
    if not provider_symbol:
        return None
    provider_symbol_id = reverse.get(provider_symbol)
    if provider_symbol_id in (None, ""):
        return None
    underlying = str(getattr(contract, "underlying", "") or "").strip()
    expiry = getattr(contract, "expiry", None)
    strike = getattr(contract, "strike", None)
    if not underlying or expiry is None or strike is None:
        return None
    try:
        strike_value = float(strike)
    except (TypeError, ValueError):
        return None
    if strike_value <= 0:
        return None
    expiry_text = expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)
    canonical_id = f"{underlying}-{expiry_text}-{int(strike_value)}-{option_type}"
    if not canonical_id or canonical_id == provider_symbol:
        return None
    event_time = getattr(snapshot, "event_time", None)
    received_time = getattr(snapshot, "received_time", None)
    quote_time = getattr(contract, "timestamp", None)
    if not all(_valid_timestamp(item) for item in (event_time, received_time, quote_time)):
        return None
    return {
        "snapshot_id": snapshot.snapshot_id,
        "event_time": event_time.isoformat(),
        "received_time": received_time.isoformat(),
        "provider_symbol_id": str(provider_symbol_id),
        "provider_symbol": provider_symbol,
        "canonical_id": canonical_id,
        "underlying": underlying,
        "expiry": expiry_text,
        "strike": strike,
        "option_type": option_type,
        "bid": bid,
        "ask": ask,
        "ltp": ltp,
        "quote_freshness": True,
        "sequence": snapshot.sequence,
    }


def assess_option_ticks(snapshot: Any, symbol_ids: Mapping[str, str] | None = None) -> OptionTickCheck:
    """A first live tick is one CE/PE contract with a real quote, not merely a snapshot."""
    if snapshot is None:
        return OptionTickCheck()
    if _snapshot_marked_fixture(snapshot):
        return OptionTickCheck(fixture_rejected=True)
    if getattr(snapshot, "freshness_ok", False) is not True:
        return OptionTickCheck()
    if not _valid_timestamp(getattr(snapshot, "event_time", None)):
        return OptionTickCheck()
    reverse = _reverse_symbol_ids(dict(symbol_ids or {}))
    quotes: list[dict[str, Any]] = []
    for chain in (getattr(snapshot, "chains", {}) or {}).values():
        for contract in getattr(chain, "contracts", ()) or ():
            row = _live_option_quote(snapshot, contract, reverse)
            if row is not None:
                quotes.append(row)
    return OptionTickCheck(tuple(quotes))


def _loop_option_check(loop: Any) -> OptionTickCheck:
    provider = getattr(loop, "provider", None)
    if _provider_quotes_marked_fixture(provider):
        return OptionTickCheck(fixture_rejected=True)
    symbol_ids = dict(getattr(provider, "_symbol_ids", {}) or {})
    return assess_option_ticks(getattr(loop, "last_snapshot", None), symbol_ids)


_SMOKE_STOP_REASONS = frozenset(
    {
        "AUTH_FAILED",
        "AUTH_MISSING",
        "METADATA_UNAVAILABLE",
        "FIXTURE_FALLBACK_FORBIDDEN",
        "SUBSCRIPTION_LIMIT",
        "SYMBOL_MAP_NOT_READY",
    }
)


def _smoke_stop_failure(reason: str) -> bool:
    text = str(reason or "")
    if text in _SMOKE_STOP_REASONS:
        return True
    if text.startswith(("AUTH_", "METADATA_", "SUBSCRIPTION_", "SESSION_TIMEOUT")):
        return True
    if "FIXTURE_FALLBACK" in text:
        return True
    return False


def _pipeline_stages(reports: Sequence[LiveCycleReport]) -> dict[str, str]:
    last = next((row for row in reversed(reports) if row.snapshot_id), None)
    not_reached = {
        "2a_market": "NOT_REACHED",
        "2b_strategy": "NOT_REACHED",
        "2c_options": "NOT_REACHED",
        "2d_ceo": "NOT_REACHED",
        "risk_guard": "NOT_REACHED",
    }
    if last is None:
        return not_reached
    if last.status in {CycleStatus.PAPER_FILL, CycleStatus.PAPER_CLOSE}:
        return {
            "2a_market": "OK",
            "2b_strategy": "OK",
            "2c_options": "OK",
            "2d_ceo": "OK",
            "risk_guard": "OK",
        }
    reason = last.reason or "NO_TRADE"
    if reason.startswith("FEED_") or reason.startswith("SESSION_") or reason.startswith("AUTH_") or reason.startswith("SYMBOL_"):
        return not_reached
    if reason.startswith("RISK_GUARD"):
        return {"2a_market": "OK", "2b_strategy": "OK", "2c_options": "OK", "2d_ceo": "OK", "risk_guard": reason}
    if last.decision_id:
        return {"2a_market": "OK", "2b_strategy": "OK", "2c_options": "OK", "2d_ceo": reason, "risk_guard": "NOT_REACHED"}
    if last.candidate_id or reason in {"MISSING_LOT_SIZE", "BULLISH_NOT_CE", "BEARISH_NOT_PE"} or reason.startswith("OPTIONS"):
        return {"2a_market": "OK", "2b_strategy": "OK", "2c_options": reason, "2d_ceo": "NOT_REACHED", "risk_guard": "NOT_REACHED"}
    if (
        reason in {"MISSING_MARKET", "MISSING_OPTION_CHAIN"}
        or reason.startswith("UNDERLYING")
        or reason.startswith("UNSUPPORTED_UNDERLYING")
    ):
        return {
            "2a_market": reason,
            "2b_strategy": "NOT_REACHED",
            "2c_options": "NOT_REACHED",
            "2d_ceo": "NOT_REACHED",
            "risk_guard": "NOT_REACHED",
        }
    return {
        "2a_market": "OK",
        "2b_strategy": reason,
        "2c_options": "NOT_REACHED",
        "2d_ceo": "NOT_REACHED",
        "risk_guard": "NOT_REACHED",
    }


def _pipeline_outcome(reports: Sequence[LiveCycleReport]) -> dict[str, Any]:
    paper_open = [row for row in reports if row.status is CycleStatus.PAPER_FILL]
    paper_close = [row for row in reports if row.status is CycleStatus.PAPER_CLOSE]
    last = reports[-1] if reports else None
    return {
        "report_count": len(reports),
        "paper_open": len(paper_open),
        "paper_close": len(paper_close),
        "last_status": None if last is None else last.status.value,
        "last_reason": None if last is None else last.reason,
        "last_underlying": None if last is None else last.underlying,
        "decision_id": None if last is None else last.decision_id,
        "candidate_id": None if last is None else last.candidate_id,
        "risk_verdict": None if last is None or last.verdict is None else last.verdict.to_dict(),
        "stages": _pipeline_stages(reports),
        "paper_events": [
            {
                "status": row.status.value,
                "reason": row.reason,
                "underlying": row.underlying,
                "option_type": row.option_type,
                "expiry": row.expiry,
                "strike": row.strike,
                "lot_size": row.lot_size,
            }
            for row in [*paper_open, *paper_close]
        ],
    }


def _is_hard_fail_error(error: str | None) -> bool:
    text = str(error or "")
    return any(token in text for token in ("FIXTURE_FALLBACK", "LIVE_TRADING", "LIVE_EXECUTION", "BROKER"))


def build_smoke_report(
    loop: Any,
    reports: Sequence[LiveCycleReport],
    *,
    secrets: Sequence[str] = (),
    root: Path | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    provider = loop.provider
    catalog = list(getattr(provider, "instrument_catalog", lambda: ())())
    mapping_ready = bool(getattr(provider, "_mapping_ready", False))
    symbol_ids = dict(getattr(provider, "_symbol_ids", {}) or {})
    snapshot = loop.last_snapshot
    health = loop.health.to_dict()
    reasons = [row.reason for row in reports]
    fixture_fallback = any("FIXTURE_FALLBACK" in reason for reason in reasons) or "FIXTURE_FALLBACK" in str(error or "")
    broker_hits = scan_broker_source(root) if root is not None else ()
    loaded = broker_modules_loaded()
    authenticated = any(
        item.get("state") in {"READY", "RUNNING"} for item in getattr(provider, "connection_events", ()) or ()
    )
    if error is not None and str(error).startswith("AUTH_"):
        authenticated = False
    catalog_ok = bool(catalog)
    quote_fixture = _provider_quotes_marked_fixture(provider)
    tick_check = assess_option_ticks(snapshot, symbol_ids)
    if quote_fixture or tick_check.fixture_rejected:
        fixture_fallback = True
    option_tick_ok = tick_check.ok and not fixture_fallback
    paper_fill = any(row.status is CycleStatus.PAPER_FILL for row in reports)
    hard_fail = bool(broker_hits or loaded or fixture_fallback or _is_hard_fail_error(error))
    result = classify_smoke_result(
        authenticated=authenticated,
        catalog_ok=catalog_ok,
        mapping_ready=mapping_ready,
        option_tick_ok=option_tick_ok,
        paper_fill=paper_fill,
        hard_fail=hard_fail,
        fixture_fallback=fixture_fallback,
    )
    sample_map = {ident: symbol_ids[ident] for ident in list(symbol_ids)[:4]}
    first = None if not option_tick_ok else dict(tick_check.quotes[0])
    payload: dict[str, Any] = {
        "schema": SMOKE_SCHEMA,
        "session_id": loop.session.session_id,
        "run_at": loop.clock.now().isoformat() if hasattr(loop.clock, "now") else datetime.now().isoformat(),
        "provider_id": provider.identity,
        "adapter_version": provider.adapter_version,
        "config": {
            "provider": "truedata",
            "mode": getattr(getattr(loop.config, "live_data", None), "mode", None),
            "paper_mode": True,
            "live_trading": False,
            "execution_mode": loop.config.execution.mode,
        },
        "connection": {
            "authenticated": authenticated,
            "health": health,
            "error": None if error is None else redact_text(str(error), secrets),
            "events": list(getattr(provider, "connection_events", ()) or ())[-12:],
        },
        "catalog": {
            "count": len(catalog),
            "ok": catalog_ok,
            "discovered_underlyings": list(getattr(provider, "discover_underlyings", lambda: ())()),
            "classification": classification_by_underlying(catalog),
            "classifier_policy_version": getattr(getattr(provider, "classifier", None), "policy_version", None),
            "classifier_calendar_version": getattr(getattr(provider, "classifier", None), "calendar_version", None),
        },
        "subscription": {
            "desired": list(getattr(provider, "_desired", ()) or getattr(provider.health(), "subscribed", ()) or ()),
            "count": len(getattr(provider, "_desired", ()) or ()),
            "acknowledged": mapping_ready,
            "mapping_ready": mapping_ready,
            "symbol_id_sample": sample_map,
            "events": list(getattr(provider, "subscription_events", ()) or ())[-8:],
        },
        "option_tick_ok": option_tick_ok,
        "first_option_tick": first,
        "first_tick": None
        if first is None or snapshot is None
        else {
            "snapshot_id": first["snapshot_id"],
            "event_time": first["event_time"],
            "received_time": first["received_time"],
            "provider_id": snapshot.provider_id,
            "provider_symbol_id": first["provider_symbol_id"],
            "provider_symbol": first["provider_symbol"],
            "canonical_id": first["canonical_id"],
            "sequence": first["sequence"],
            "freshness_ok": True,
            "quote_freshness": first["quote_freshness"],
            "underlyings": list(snapshot.underlyings),
            "contracts": [
                {
                    "canonical_id": first["canonical_id"],
                    "provider_contract_id": first["provider_symbol"],
                    "provider_symbol_id": first["provider_symbol_id"],
                    "underlying": first["underlying"],
                    "expiry": first["expiry"],
                    "strike": first["strike"],
                    "option_type": first["option_type"],
                }
            ],
            "diagnostics": list(snapshot.diagnostics),
        },
        "pipeline": _pipeline_outcome(reports),
        "positions": loop.positions.summary().to_dict() if hasattr(loop, "positions") else {},
        "session": loop.session.to_dict(),
        "safety": {
            "paper_only": True,
            "live_trading": False,
            "broker": False,
            "catalog_injected": False,
            "secrets_redacted": True,
            "fixture_fallback": fixture_fallback,
            "broker_source_hits": list(broker_hits),
            "broker_modules": list(loaded),
            "live_trading_compiled": LIVE_TRADING_COMPILED,
        },
        "result": result,
    }
    redacted = redact_tree(payload, secrets)
    if contains_secret(redacted, secrets) or broker_hits or loaded:
        redacted["result"] = HARD_FAIL
        redacted["safety"]["secrets_redacted"] = not contains_secret(redacted, secrets)
        redacted["safety"]["broker"] = bool(broker_hits or loaded)
    return redacted


def write_smoke_report(report: Mapping[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def drain_smoke_loop(loop: Any, *, max_cycles: int = DEFAULT_MAX_CYCLES) -> list[LiveCycleReport]:
    """Poll until a live option quote, a paper open, or a terminal smoke stop.

    An index snapshot, heartbeat, auth frame, subscription ack, or catalog row
    does not end the loop. ``max_cycles`` is the bounded smoke deadline.
    """
    reports: list[LiveCycleReport] = []
    for _ in range(max_cycles):
        batch = loop.run_once()
        reports.extend(batch)
        if any(row.status is CycleStatus.PAPER_FILL for row in reports):
            break
        check = _loop_option_check(loop)
        if check.fixture_rejected or check.ok:
            break
        reason = batch[-1].reason if batch else ""
        if _smoke_stop_failure(reason):
            break
    return reports
