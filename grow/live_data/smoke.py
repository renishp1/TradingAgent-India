"""3C.3 TrueData option-tick evidence. Paper only. No secrets. No broker."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field, replace
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
    "grow/live_data/kite_market.py",
    "grow/live_data/loop.py",
    "grow/live_data/smoke.py",
    "grow/live_data/provider.py",
    "scripts/run_truedata_smoke.py",
    "scripts/run_" + _tok("zero", "dha") + "_smoke.py",
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


_MARKET_DATA_ENV = ("KITE_API_KEY", "KITE_ACCESS_TOKEN")


def kite_market_smoke_config(environ: Mapping[str, str] | None = None) -> GrowConfig:
    """Paper Kite market-data smoke. Credentials never enter the boot lock."""
    env = dict(environ or {})
    if str(env.get("ZERODHA_SMOKE") or "").strip() != "1":
        raise GrowConfigError("SMOKE_DISABLED: set ZERODHA_SMOKE=1 for a real Zerodha smoke run")
    stripped = {key: value for key, value in env.items() if key not in _MARKET_DATA_ENV}
    timeout = int(str(stripped.get("ZERODHA_SMOKE_TIMEOUT") or DEFAULT_TIMEOUT_SECONDS).strip() or DEFAULT_TIMEOUT_SECONDS)
    if timeout < 1:
        raise GrowConfigError("ZERODHA_SMOKE_TIMEOUT must be >= 1")
    if LIVE_TRADING_COMPILED:
        raise GrowLiveTradingDisabled("LIVE_TRADING_COMPILED forbids 3C.3")
    inspect_environment(stripped.items())
    forced = {
        **stripped,
        "GROW_LIVE_DATA_ENABLED": "true",
        "GROW_LIVE_DATA_PROVIDER": "kite_market",
        "GROW_LIVE_DATA_MODE": "real",
        "LIVE_DATA_ENABLED": "true",
        "LIVE_DATA_PROVIDER": "kite_market",
        "GROW_LIVE_DATA_LIVE_TRADING": "false",
        "GROW_LIVE_DATA_PAPER_MODE": "true",
    }
    config = load_config(environ=forced)
    live = replace(
        config.live_data,
        enabled=True,
        provider="kite_market",
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


MAX_OPTION_REJECTION_SAMPLES = 20


@dataclass(frozen=True)
class OptionTickCheck:
    """Accepted live quotes only.

    ``quotes`` are the contracts that passed assessment. Rejection fields are
    diagnostics: they never enter ``quotes``, never become ``first_option_tick``,
    and never set ``option_tick_ok``. One accepted CE/PE quote stays accepted
    when other candidate contracts are rejected. Samples are capped; counts
    still record every rejection.
    """

    quotes: tuple[Mapping[str, Any], ...] = ()
    fixture_rejected: bool = False
    rejection_count: int = 0
    primary_rejection: str | None = None
    rejection_counts: Mapping[str, int] = field(default_factory=dict)
    rejection_samples: tuple[Mapping[str, Any], ...] = ()

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


NO_OPTION_QUOTE = "no_option_quote"
STALE_OPTION_QUOTE = "stale_option_quote"
FUTURE_OPTION_QUOTE = "future_option_quote"
QUOTE_AFTER_EVENT = "quote_after_event_time"
MISSING_SYMBOL_MAPPING = "missing_symbol_mapping"
FIXTURE_QUOTE = "fixture_quote"
INVALID_OPTION_TYPE = "invalid_option_type"
INVALID_QUOTE_TIMESTAMP = "invalid_quote_timestamp"


def _option_quote_freshness_failure(
    quote_time: Any,
    event_time: Any,
    now: Any,
    max_staleness_seconds: int | None,
) -> str | None:
    """Why a quote fails 3C.2 freshness. None means the quote is fresh.

    Snapshot freshness_ok is not consulted here.
    """
    if max_staleness_seconds is None:
        return INVALID_QUOTE_TIMESTAMP
    if not all(_valid_timestamp(item) for item in (quote_time, event_time, now)):
        return INVALID_QUOTE_TIMESTAMP
    try:
        limit = float(max_staleness_seconds)
    except (TypeError, ValueError):
        return INVALID_QUOTE_TIMESTAMP
    if quote_time > now:
        return FUTURE_OPTION_QUOTE
    if quote_time > event_time:
        return QUOTE_AFTER_EVENT
    age = (now - quote_time).total_seconds()
    if age > limit:
        return STALE_OPTION_QUOTE
    return None


def _option_quote_is_fresh(
    quote_time: Any,
    event_time: Any,
    now: Any,
    max_staleness_seconds: int | None,
) -> bool:
    """Per-contract freshness. Snapshot freshness_ok is not a substitute."""
    return _option_quote_freshness_failure(quote_time, event_time, now, max_staleness_seconds) is None


def _staleness_limit(loop: Any) -> int | None:
    live = getattr(getattr(loop, "config", None), "live_data", None)
    raw = getattr(live, "max_staleness_seconds", None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _evaluation_now(loop: Any) -> datetime | None:
    clock = getattr(loop, "clock", None)
    if clock is None or not hasattr(clock, "now"):
        return None
    now = clock.now()
    if _valid_timestamp(now):
        return now
    return None


def _diagnostic(reason: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"reason": reason}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        row[key] = value
    return row


def _quote_age_ms(quote_time: Any, now: Any) -> int | None:
    if not _valid_timestamp(quote_time) or not _valid_timestamp(now):
        return None
    return int(round((now - quote_time).total_seconds() * 1000))


def _live_option_quote(
    snapshot: Any,
    contract: Any,
    reverse: Mapping[str, str],
    *,
    now: datetime | None,
    max_staleness_seconds: int | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (accepted quote, rejection). A rejection is never an accepted tick."""
    option_type = str(_scalar(getattr(contract, "option_type", "")) or "")
    bid = getattr(contract, "bid", None)
    ask = getattr(contract, "ask", None)
    ltp = getattr(contract, "last_price", None)
    priced = bid is not None or ask is not None or ltp is not None
    if option_type not in {"CE", "PE"}:
        if not priced:
            return None, None
        return None, _diagnostic(INVALID_OPTION_TYPE, option_type=option_type or None)
    if not priced:
        return None, None
    provider_symbol = str(getattr(contract, "provider_contract_id", "") or "").strip()
    provider_symbol_id = reverse.get(provider_symbol) if provider_symbol else None
    underlying = str(getattr(contract, "underlying", "") or "").strip()
    expiry = getattr(contract, "expiry", None)
    strike = getattr(contract, "strike", None)
    expiry_text = expiry.isoformat() if hasattr(expiry, "isoformat") else (None if expiry is None else str(expiry))
    canonical_id = None
    if underlying and expiry_text and strike is not None:
        try:
            canonical_id = f"{underlying}-{expiry_text}-{int(float(strike))}-{option_type}"
        except (TypeError, ValueError):
            canonical_id = None
    event_time = getattr(snapshot, "event_time", None)
    received_time = getattr(snapshot, "received_time", None)
    quote_time = getattr(contract, "timestamp", None)
    detail = _diagnostic(
        "",
        option_type=option_type,
        provider_symbol=provider_symbol or None,
        provider_symbol_id=None if provider_symbol_id in (None, "") else str(provider_symbol_id),
        canonical_id=canonical_id,
        underlying=underlying or None,
        expiry=expiry_text,
        strike=strike,
        quote_timestamp=quote_time.isoformat() if _valid_timestamp(quote_time) else None,
        event_time=event_time.isoformat() if _valid_timestamp(event_time) else None,
        quote_age_ms=_quote_age_ms(quote_time, now),
    )
    detail.pop("reason", None)
    if not provider_symbol or provider_symbol_id in (None, ""):
        return None, _diagnostic(MISSING_SYMBOL_MAPPING, **detail)
    if canonical_id is None or canonical_id == provider_symbol:
        return None, _diagnostic(NO_OPTION_QUOTE, **detail)
    if not _valid_timestamp(received_time):
        return None, _diagnostic(INVALID_QUOTE_TIMESTAMP, **detail)
    failure = _option_quote_freshness_failure(quote_time, event_time, now, max_staleness_seconds)
    if failure is not None:
        return None, _diagnostic(failure, **detail)
    age_seconds = (now - quote_time).total_seconds()
    accepted = {
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
        "quote_timestamp": quote_time.isoformat(),
        "quote_age_seconds": age_seconds,
        "quote_age_ms": int(round(age_seconds * 1000)),
        "quote_freshness": True,
        "sequence": snapshot.sequence,
    }
    return accepted, None


def _note_rejection(
    rejection: Mapping[str, Any],
    *,
    primary: list[str | None],
    counts: dict[str, int],
    samples: list[dict[str, Any]],
) -> None:
    reason = str(rejection.get("reason") or NO_OPTION_QUOTE)
    if primary[0] is None:
        primary[0] = reason
    counts[reason] = counts.get(reason, 0) + 1
    if len(samples) < MAX_OPTION_REJECTION_SAMPLES:
        samples.append(dict(rejection))


def _rejection_check(
    *,
    quotes: Sequence[Mapping[str, Any]] = (),
    fixture_rejected: bool = False,
    primary: str | None = None,
    counts: Mapping[str, int] | None = None,
    samples: Sequence[Mapping[str, Any]] = (),
    count: int | None = None,
) -> OptionTickCheck:
    grouped = dict(counts or {})
    total = sum(grouped.values()) if count is None else count
    return OptionTickCheck(
        quotes=tuple(quotes),
        fixture_rejected=fixture_rejected,
        rejection_count=total,
        primary_rejection=primary,
        rejection_counts=grouped,
        rejection_samples=tuple(samples),
    )


def _one_rejection(reason: str, *, fixture_rejected: bool = False) -> OptionTickCheck:
    return _rejection_check(
        fixture_rejected=fixture_rejected,
        primary=reason,
        counts={reason: 1},
        samples=({"reason": reason},),
        count=1,
    )


def assess_option_ticks(
    snapshot: Any,
    symbol_ids: Mapping[str, str] | None = None,
    *,
    max_staleness_seconds: int | None = None,
    now: datetime | None = None,
) -> OptionTickCheck:
    """A first live tick is one CE/PE contract with a fresh quote, not merely a snapshot."""
    if snapshot is None:
        return _one_rejection(NO_OPTION_QUOTE)
    if _snapshot_marked_fixture(snapshot):
        return _one_rejection(FIXTURE_QUOTE, fixture_rejected=True)
    if getattr(snapshot, "freshness_ok", False) is not True:
        return _one_rejection(NO_OPTION_QUOTE)
    if not _valid_timestamp(getattr(snapshot, "event_time", None)):
        return _one_rejection(NO_OPTION_QUOTE)
    reverse = _reverse_symbol_ids(dict(symbol_ids or {}))
    quotes: list[dict[str, Any]] = []
    primary: list[str | None] = [None]
    counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    seen = 0
    for chain in (getattr(snapshot, "chains", {}) or {}).values():
        for contract in getattr(chain, "contracts", ()) or ():
            accepted, rejection = _live_option_quote(
                snapshot,
                contract,
                reverse,
                now=now,
                max_staleness_seconds=max_staleness_seconds,
            )
            if accepted is not None:
                quotes.append(accepted)
            elif rejection is not None:
                seen += 1
                _note_rejection(rejection, primary=primary, counts=counts, samples=samples)
    if not quotes and seen == 0:
        return _one_rejection(NO_OPTION_QUOTE)
    return _rejection_check(
        quotes=quotes,
        primary=primary[0],
        counts=counts,
        samples=samples,
        count=seen,
    )


def _loop_option_check(loop: Any) -> OptionTickCheck:
    provider = getattr(loop, "provider", None)
    if _provider_quotes_marked_fixture(provider):
        return _one_rejection(FIXTURE_QUOTE, fixture_rejected=True)
    symbol_ids = dict(getattr(provider, "_symbol_ids", {}) or {})
    return assess_option_ticks(
        getattr(loop, "last_snapshot", None),
        symbol_ids,
        max_staleness_seconds=_staleness_limit(loop),
        now=_evaluation_now(loop),
    )


_SMOKE_STOP_REASONS = frozenset(
    {
        "AUTH_FAILED",
        "AUTH_MISSING",
        "METADATA_UNAVAILABLE",
        "FIXTURE_FALLBACK_FORBIDDEN",
        "SUBSCRIPTION_LIMIT",
        "SYMBOL_MAP_NOT_READY",
        "NO_VALID_OPTION",
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


def _smoke_rejection_diagnostics(check: OptionTickCheck, *, fixture_fallback: bool) -> dict[str, Any]:
    """Diagnostics only. They do not accept a quote or clear an accepted one."""
    counts = dict(check.rejection_counts)
    samples = [dict(item) for item in check.rejection_samples]
    total = check.rejection_count
    primary = check.primary_rejection
    if fixture_fallback and counts.get(FIXTURE_QUOTE, 0) == 0:
        counts[FIXTURE_QUOTE] = 1
        total += 1
        primary = FIXTURE_QUOTE
        samples = [{"reason": FIXTURE_QUOTE}, *samples][:MAX_OPTION_REJECTION_SAMPLES]
    elif fixture_fallback:
        primary = FIXTURE_QUOTE
    if total == 0 and not check.ok:
        counts = {NO_OPTION_QUOTE: 1}
        total = 1
        primary = NO_OPTION_QUOTE
        samples = [{"reason": NO_OPTION_QUOTE}]
    if total == 0:
        primary = None
    return {
        "option_tick_rejection": primary,
        "option_tick_rejection_count": total,
        "option_tick_rejection_counts": counts,
        "option_tick_rejection_samples": samples,
    }


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
    tick_check = assess_option_ticks(
        snapshot,
        symbol_ids,
        max_staleness_seconds=_staleness_limit(loop),
        now=_evaluation_now(loop),
    )
    if quote_fixture or tick_check.fixture_rejected:
        fixture_fallback = True
    option_tick_ok = tick_check.ok and not fixture_fallback
    rejection_diagnostics = _smoke_rejection_diagnostics(tick_check, fixture_fallback=fixture_fallback)
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
            "provider": _report_provider_name(provider),
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
        "option_tick_rejection": rejection_diagnostics["option_tick_rejection"],
        "option_tick_rejection_count": rejection_diagnostics["option_tick_rejection_count"],
        "option_tick_rejection_counts": rejection_diagnostics["option_tick_rejection_counts"],
        "option_tick_rejection_samples": rejection_diagnostics["option_tick_rejection_samples"],
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
            "quote_timestamp": first["quote_timestamp"],
            "quote_age_seconds": first["quote_age_seconds"],
            "quote_age_ms": first["quote_age_ms"],
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


def _report_provider_name(provider: Any) -> str:
    identity = str(getattr(provider, "identity", "") or "")
    if "kite.market" in identity:
        return "kite_market"
    if "mock" in identity:
        return "mock"
    return "truedata"


def _evidence_provider_name(report: Mapping[str, Any]) -> str:
    config = report.get("config") if isinstance(report.get("config"), Mapping) else {}
    blob = f"{report.get('provider_id') or ''} {config.get('provider') or ''}".lower()
    if "kite_market" in blob or "kite.market" in blob:
        return "Zerodha"
    return "TrueData"


def _shown(value: Any) -> str:
    if value is None or value == "":
        return "absent"
    return str(value)


def format_option_tick_evidence(report: Mapping[str, Any]) -> str:
    """Auditable 3C.3 lines. Does not decide freshness or rewrite timestamps."""
    raw_tick = report.get("first_option_tick")
    tick = raw_tick if isinstance(raw_tick, Mapping) else {}
    result = str(report.get("result") or FAIL)
    option_ok = report.get("option_tick_ok") is True
    subscription = report.get("subscription")
    safety = report.get("safety")
    sub = subscription if isinstance(subscription, Mapping) else {}
    safe = safety if isinstance(safety, Mapping) else {}
    mapped = bool(sub.get("mapping_ready")) and bool(tick.get("provider_symbol_id"))
    fixture_clean = safe.get("fixture_fallback") is False
    fresh = tick.get("quote_freshness") is True
    proven = option_ok and result in {PASS, PASS_WITH_NO_TRADE} and fresh and mapped and fixture_clean
    age = tick.get("quote_age_ms")
    age_text = "absent" if age is None else f"{age} ms"
    lines = [
        f"OPTION TICK: {'PASS' if proven else 'FAIL'}",
        f"Provider: {_evidence_provider_name(report)}",
        f"Underlying: {_shown(tick.get('underlying'))}",
        f"Expiry: {_shown(tick.get('expiry'))}",
        f"Strike: {_shown(tick.get('strike'))}",
        f"Option: {_shown(tick.get('option_type'))}",
        f"Provider Symbol ID: {_shown(tick.get('provider_symbol_id'))}",
        f"Instrument token: {_shown(tick.get('provider_symbol_id'))}",
        f"Provider Symbol: {_shown(tick.get('provider_symbol'))}",
        f"Canonical ID: {_shown(tick.get('canonical_id'))}",
        f"Quote timestamp: {_shown(tick.get('quote_timestamp'))}",
        f"Snapshot timestamp: {_shown(tick.get('event_time'))}",
        f"Received timestamp: {_shown(tick.get('received_time'))}",
        f"Quote age: {age_text}",
        f"LTP: {_shown(tick.get('ltp'))}",
        f"Bid: {_shown(tick.get('bid'))}",
        f"Ask: {_shown(tick.get('ask'))}",
        f"Quote freshness: {'PASS' if fresh else 'FAIL'}",
        f"Symbol mapping: {'PASS' if mapped else 'FAIL'}",
        f"Fixture detection: {'PASS' if fixture_clean else 'FAIL'}",
        f"Option tick: {'PASS' if option_ok else 'FAIL'}",
        f"Smoke result: {result}",
    ]
    rejection = report.get("option_tick_rejection")
    if rejection:
        lines.append(f"Rejection: {rejection}")
    raw_rejections = report.get("option_tick_rejection_samples")
    detail = raw_rejections[0] if isinstance(raw_rejections, list) and raw_rejections else None
    if isinstance(detail, Mapping):
        if detail.get("quote_timestamp"):
            lines.append(f"Rejected quote timestamp: {detail['quote_timestamp']}")
        if detail.get("quote_age_ms") is not None:
            lines.append(f"Rejected quote age: {detail['quote_age_ms']} ms")
    return "\n".join(lines)


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
