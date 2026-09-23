"""Zerodha market-data live proofs for the production safety checklist.

Market-data only. Never places broker orders. Never flips LIVE_TRADING_COMPILED.
Outcomes per proof: PASS | FAIL | BLOCKED (AUTH_MISSING / config).
"""

from __future__ import annotations

import ast
import inspect
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Mapping, Sequence

from grow.clock import Clock, SystemClock
from grow.errors import GrowConfigError, GrowLiveTradingDisabled
from grow.execution.lock import LIVE_TRADING_COMPILED, assert_paper_compiled, inspect_environment
from grow.live_data.kite_market import KiteMarketProvider, load_kite_market_secrets
from grow.live_data.models import SessionHealth
from grow.live_data.normalize import normalize_event
from grow.live_data.smoke import assess_option_ticks, kite_market_smoke_config
from grow.market.session import SessionCalendar


LIVE_PROOF_IDS: tuple[str, ...] = (
    "real_zerodha_ce_tick",
    "real_zerodha_pe_tick",
    "websocket_reconnect_live_proof",
    "real_option_chain_live_proof",
)

_AUTH_BLOCK_REASONS = frozenset(
    {
        "AUTH_MISSING",
        "DEPENDENCY_MISSING",
        "SMOKE_DISABLED",
    }
)
_MARKET_DATA_ENV = ("KITE_API_KEY", "KITE_ACCESS_TOKEN")
_DEFAULT_POLL_ATTEMPTS = 64
_DEFAULT_RECONNECT_WAIT_SECONDS = 2.0


@dataclass(frozen=True)
class LiveProofResult:
    id: str
    status: str  # PASS | FAIL | BLOCKED
    detail: str
    evidence: str

    def to_checklist_tuple(self) -> tuple[str, str, str, str]:
        return self.id, self.status, self.detail, self.evidence


@dataclass(frozen=True)
class LiveProofHooks:
    """Injectable seams for deterministic unit tests."""

    load_secrets: Callable[[Mapping[str, str]], tuple[str, str]] | None = None
    open_provider: Callable[[str, str, Clock], KiteMarketProvider] | None = None
    poll_attempts: int = _DEFAULT_POLL_ATTEMPTS
    reconnect_wait_seconds: float = _DEFAULT_RECONNECT_WAIT_SECONDS


def _blocked(item_id: str, detail: str, evidence: str) -> LiveProofResult:
    return LiveProofResult(item_id, "BLOCKED", detail, evidence)


def _pass(item_id: str, detail: str, evidence: str) -> LiveProofResult:
    return LiveProofResult(item_id, "PASS", detail, evidence)


def _fail(item_id: str, detail: str, evidence: str) -> LiveProofResult:
    return LiveProofResult(item_id, "FAIL", detail, evidence)


def _is_auth_block(reason: str) -> bool:
    text = str(reason or "")
    return any(token in text for token in _AUTH_BLOCK_REASONS)


def _public_reason(exc: BaseException) -> str:
    if isinstance(exc, (GrowConfigError, GrowLiveTradingDisabled)):
        return str(exc)
    return "NETWORK_ERROR"


def _all_blocked(reason: str, evidence: str) -> tuple[LiveProofResult, ...]:
    detail = f"Live market-data proof not attempted ({reason})"
    return tuple(_blocked(item_id, detail, evidence) for item_id in LIVE_PROOF_IDS)


def _all_failed(reason: str, evidence: str) -> tuple[LiveProofResult, ...]:
    detail = f"Live market-data proof attempted but failed ({reason})"
    return tuple(_fail(item_id, detail, evidence) for item_id in LIVE_PROOF_IDS)


def _advance_clock(clock: Clock, seconds: float) -> None:
    if hasattr(clock, "advance"):
        clock.advance(timedelta(seconds=seconds))  # type: ignore[attr-defined]
    else:
        time.sleep(max(0.0, seconds))


def _quote_usable(quote: Mapping[str, Any], *, option_type: str) -> bool:
    if str(quote.get("option_type") or "") != option_type:
        return False
    if quote.get("is_fixture") is True:
        return False
    if not quote.get("underlying"):
        return False
    if not quote.get("expiry"):
        return False
    if quote.get("strike") is None:
        return False
    if quote.get("ltp") is None and quote.get("bid") is None and quote.get("ask") is None:
        return False
    if not quote.get("provider_symbol") and not quote.get("canonical_id"):
        return False
    if not quote.get("quote_timestamp") and not quote.get("ts"):
        return False
    return True


def _quote_matches_selected(quote: Mapping[str, Any], selected: Any) -> bool:
    """Require the tick to be the exact selected contract (token preferred)."""
    if selected is None:
        return False
    token = str(quote.get("provider_symbol_id") or "").strip()
    if token and token == str(selected.instrument_token):
        return True
    symbol = str(quote.get("provider_symbol") or "").strip()
    if symbol and symbol == str(selected.tradingsymbol):
        return True
    return False


def _quote_fingerprint(quote: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(quote.get("provider_symbol_id") or ""),
        str(quote.get("provider_symbol") or quote.get("canonical_id") or ""),
        str(quote.get("quote_timestamp") or quote.get("ts") or ""),
        str(quote.get("ltp")),
        str(quote.get("bid")),
        str(quote.get("ask")),
        str(quote.get("option_type") or ""),
    )


def _evidence_quote(quote: Mapping[str, Any], *, selected: Any | None = None) -> str:
    parts = [
        f"selected_contract={getattr(selected, 'tradingsymbol', None)}",
        f"selected_token={getattr(selected, 'instrument_token', None)}",
        f"received_contract={quote.get('provider_symbol') or quote.get('canonical_id')}",
        f"received_token={quote.get('provider_symbol_id')}",
        f"underlying={quote.get('underlying')}",
        f"option_type={quote.get('option_type')}",
        f"expiry={quote.get('expiry')}",
        f"strike={quote.get('strike')}",
        f"ltp={quote.get('ltp')}",
        f"bid={quote.get('bid')}",
        f"ask={quote.get('ask')}",
        f"ts={quote.get('quote_timestamp') or quote.get('ts')}",
        f"is_fixture={quote.get('is_fixture')}",
    ]
    return " ".join(parts)


def _open_real_provider(api_key: str, access_token: str, clock: Clock) -> KiteMarketProvider:
    provider = KiteMarketProvider(api_key=api_key, access_token=access_token, clock=clock)
    provider.connect()
    return provider


def _poll_quotes(
    provider: KiteMarketProvider,
    *,
    attempts: int,
    max_staleness_seconds: int,
    calendar: SessionCalendar,
) -> list[dict[str, Any]]:
    """Drain provider polls and return accepted live option quotes."""
    accepted: list[dict[str, Any]] = []
    for _ in range(max(1, attempts)):
        raw = provider.poll()
        if not isinstance(raw, dict) or raw.get("kind") in {"control", "heartbeat"}:
            continue
        if "option_quotes" not in raw or not raw.get("option_quotes"):
            continue
        try:
            snapshot = normalize_event(
                raw,
                now=provider.clock.now(),
                max_staleness_seconds=max_staleness_seconds,
                calendar=calendar,
            )
        except GrowConfigError:
            continue
        check = assess_option_ticks(
            snapshot,
            provider._symbol_ids,
            max_staleness_seconds=max_staleness_seconds,
            now=provider.clock.now(),
        )
        for quote in check.quotes:
            row = dict(quote)
            if row.get("is_fixture") is True:
                continue
            accepted.append(row)
        if accepted:
            break
    return accepted


def _find_selected_quote(
    quotes: Sequence[Mapping[str, Any]],
    *,
    selected: Any,
    option_type: str,
) -> dict[str, Any] | None:
    for quote in quotes:
        if not _quote_usable(quote, option_type=option_type):
            continue
        if _quote_matches_selected(quote, selected):
            return dict(quote)
    return None


def _prove_side_tick(
    *,
    item_id: str,
    option_type: str,
    provider: KiteMarketProvider,
    quotes: Sequence[Mapping[str, Any]],
) -> LiveProofResult:
    selected = provider.selected if option_type == "CE" else provider.selected_put
    if selected is None or selected.option_type != option_type:
        return _fail(
            item_id,
            f"No live {option_type} contract selected after authenticate/subscribe",
            f"selected_{option_type.lower()}=None",
        )
    quote = _find_selected_quote(quotes, selected=selected, option_type=option_type)
    if quote is None:
        # Distinguish "no tick" vs "wrong contract tick".
        same_side = [
            row
            for row in quotes
            if _quote_usable(row, option_type=option_type) and not _quote_matches_selected(row, selected)
        ]
        if same_side:
            other = same_side[0]
            return _fail(
                item_id,
                f"Live {option_type} tick did not match selected contract",
                (
                    f"selected_contract={selected.tradingsymbol} selected_token={selected.instrument_token} "
                    f"received_contract={other.get('provider_symbol') or other.get('canonical_id')} "
                    f"received_token={other.get('provider_symbol_id')}"
                ),
            )
        return _fail(
            item_id,
            f"Authenticated session did not yield a usable live {option_type} tick for selected contract",
            f"selected_contract={selected.tradingsymbol} selected_token={selected.instrument_token}",
        )
    return _pass(
        item_id,
        f"Received real live {option_type} tick for selected {selected.tradingsymbol}",
        _evidence_quote(quote, selected=selected),
    )


def _prove_option_chain(provider: KiteMarketProvider) -> LiveProofResult:
    item_id = "real_option_chain_live_proof"
    chain = tuple(provider._chain or ())
    catalog = provider.instrument_catalog()
    if not chain or not catalog:
        return _fail(
            item_id,
            "Live option chain empty after authenticate",
            f"chain_len={len(chain)} catalog_len={len(catalog)}",
        )
    ce_rows = [row for row in chain if row.option_type == "CE"]
    pe_rows = [row for row in chain if row.option_type == "PE"]
    if not ce_rows or not pe_rows:
        return _fail(
            item_id,
            "Live option chain missing CE or PE contracts",
            f"ce={len(ce_rows)} pe={len(pe_rows)} underlying={getattr(provider.selected, 'underlying', None)}",
        )
    sample = chain[0]
    # Identity / expiry / strike / non-fixture provenance on selected pair + catalog.
    if provider.selected is None or provider.selected_put is None:
        return _fail(item_id, "Selected CE/PE pair missing for chain proof", "selected_pair=None")
    call = provider.selected
    put = provider.selected_put
    if call.expiry != put.expiry or call.strike != put.strike:
        return _fail(
            item_id,
            "Selected CE/PE pair identity mismatch",
            f"ce={call.tradingsymbol} pe={put.tradingsymbol}",
        )
    if call.underlying != put.underlying:
        return _fail(item_id, "Selected CE/PE underlying mismatch", f"ce={call.underlying} pe={put.underlying}")
    for row in (call, put):
        if row.strike is None or row.expiry is None or not row.tradingsymbol:
            return _fail(item_id, "Selected contract missing identity fields", row.tradingsymbol)
    evidence = (
        f"underlying={call.underlying} expiry={call.expiry.isoformat()} strike={call.strike} "
        f"ce={call.tradingsymbol} pe={put.tradingsymbol} chain_contracts={len(chain)} "
        f"catalog_rows={len(catalog)} sample={sample.tradingsymbol} provenance=kite.market.live"
    )
    return _pass(
        item_id,
        f"Validated live option chain with CE/PE for {call.underlying} {call.expiry.isoformat()}",
        evidence,
    )


def _prove_websocket_reconnect(
    provider: KiteMarketProvider,
    *,
    attempts: int,
    max_staleness_seconds: int,
    reconnect_wait_seconds: float,
    calendar: SessionCalendar,
    pre_reconnect_quotes: Sequence[Mapping[str, Any]] = (),
) -> LiveProofResult:
    item_id = "websocket_reconnect_live_proof"
    transport = provider.transport
    if transport is None or not getattr(transport, "connected", False):
        return _fail(item_id, "WebSocket transport not connected before reconnect proof", "transport=None")
    before = int(provider.reconnect_count)
    pre_fps = {_quote_fingerprint(row) for row in pre_reconnect_quotes}
    # Confirm initial data flow already happened (caller supplies prior quotes) via RUNNING/READY.
    if provider.health().state not in {SessionHealth.READY, SessionHealth.RUNNING}:
        return _fail(
            item_id,
            f"Provider not healthy before reconnect (state={provider.health().state.value})",
            f"reconnect_count={before}",
        )
    if not pre_reconnect_quotes:
        return _fail(
            item_id,
            "Reconnect proof requires initial valid live market data before disconnect",
            f"reconnect_count={before}",
        )
    # Force disconnect → FEED_DISCONNECTED → bounded reconnect path.
    transport.disconnect()
    # First poll should enter reconnect backoff.
    try:
        provider.poll()
    except GrowConfigError as exc:
        if _is_auth_block(str(exc)):
            return _blocked(item_id, f"Reconnect blocked ({exc})", "AUTH_MISSING")
        if provider.health().state is SessionHealth.STOPPED:
            return _fail(item_id, f"Reconnect aborted ({exc})", str(exc))
    _advance_clock(provider.clock, max(reconnect_wait_seconds, 1.0))

    after = before
    connected = False
    subscribed: tuple[Any, ...] = ()
    new_quotes: list[dict[str, Any]] = []
    try:
        for _ in range(max(1, attempts)):
            provider.poll()
            after = int(provider.reconnect_count)
            connected = bool(getattr(transport, "connected", False))
            subscribed = tuple(getattr(transport, "subscribed", ()) or ())
            if after <= before or not connected or not subscribed:
                _advance_clock(provider.clock, 0.05)
                continue
            if provider.health().state not in {SessionHealth.READY, SessionHealth.RUNNING}:
                _advance_clock(provider.clock, 0.05)
                continue
            # Only quotes received after reconnect count increases count as post-reconnect.
            polled = _poll_quotes(
                provider,
                attempts=max(1, attempts // 2),
                max_staleness_seconds=max_staleness_seconds,
                calendar=calendar,
            )
            new_quotes = [row for row in polled if _quote_fingerprint(row) not in pre_fps]
            if new_quotes:
                break
            _advance_clock(provider.clock, 0.05)
    except GrowConfigError as exc:
        if _is_auth_block(str(exc)):
            return _blocked(item_id, f"Reconnect blocked ({exc})", str(exc))
        return _fail(item_id, f"Reconnect path failed ({exc})", str(exc))

    after = int(provider.reconnect_count)
    connected = bool(getattr(transport, "connected", False))
    subscribed = tuple(getattr(transport, "subscribed", ()) or ())
    if after <= before or not connected or not subscribed:
        return _fail(
            item_id,
            "Reconnect did not restore websocket subscription",
            f"reconnect_before={before} after={after} connected={connected} subscribed={list(subscribed)}",
        )
    if not new_quotes:
        return _fail(
            item_id,
            "Reconnect restored subscription but no NEW post-reconnect market-data tick arrived",
            (
                f"reconnect_count={after} subscribed={list(subscribed)} "
                f"state={provider.health().state.value} pre_quotes={len(pre_fps)} post_new=0"
            ),
        )
    sample = new_quotes[0]
    return _pass(
        item_id,
        "WebSocket reconnect restored live market-data flow with new post-reconnect tick",
        (
            f"reconnect_count={after} subscribed={list(subscribed)} "
            f"post_reconnect_quote={_evidence_quote(sample)}"
        ),
    )


def assert_live_proofs_forbid_broker_orders() -> None:
    """Static guard: this module must not call broker order APIs."""
    import sys

    module = sys.modules[__name__]
    source = inspect.getsource(module)
    tree = ast.parse(source)
    forbidden = {"place_order", "place_live_order", "LiveBroker"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in forbidden:
            raise GrowLiveTradingDisabled("live proofs must not call broker order APIs")
        if isinstance(func, ast.Attribute) and func.attr in forbidden:
            raise GrowLiveTradingDisabled("live proofs must not call broker order APIs")
        if isinstance(func, ast.Name) and func.id == "LiveBroker":
            raise GrowLiveTradingDisabled("live proofs must not instantiate LiveBroker")


def _module_has_broker_calls(source: str) -> bool:
    tree = ast.parse(source)
    forbidden = {"place_order", "place_live_order"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden | {"LiveBroker"}:
                return True
            if isinstance(func, ast.Attribute) and func.attr in forbidden:
                return True
    return False


def run_zerodha_live_proofs(
    *,
    environ: Mapping[str, str] | None = None,
    hooks: LiveProofHooks | None = None,
    clock: Clock | None = None,
) -> tuple[LiveProofResult, ...]:
    """Execute the four Zerodha market-data proofs.

    BLOCKED — credentials/config missing (AUTH_MISSING or equivalent).
    PASS — real proof executed successfully with evidence.
    FAIL — credentials present, proof attempted, validation failed.
    """
    assert_paper_compiled()
    assert_live_proofs_forbid_broker_orders()
    if LIVE_TRADING_COMPILED:
        return _all_failed("LIVE_TRADING_COMPILED", "grow.execution.lock")

    env = dict(os.environ if environ is None else environ)
    hooks = hooks or LiveProofHooks()
    load_secrets = hooks.load_secrets or load_kite_market_secrets
    open_provider = hooks.open_provider or _open_real_provider
    proof_clock = clock or SystemClock()

    # Credentials gate — missing → BLOCKED, never pretend PASS.
    try:
        api_key, access_token = load_secrets(env)
    except GrowConfigError as exc:
        reason = str(exc)
        if _is_auth_block(reason):
            return _all_blocked(reason if reason else "AUTH_MISSING", "load_kite_market_secrets")
        return _all_failed(reason, "load_kite_market_secrets")
    except Exception as exc:  # noqa: BLE001
        return _all_failed(_public_reason(exc), "load_kite_market_secrets")

    if not api_key or not access_token:
        return _all_blocked("AUTH_MISSING", "empty credentials after load")

    # Paper-only boot lock without broker-token smell.
    stripped = {key: value for key, value in env.items() if key not in _MARKET_DATA_ENV}
    try:
        inspect_environment(stripped.items())
        # Prefer kite smoke config when ZERODHA_SMOKE=1; otherwise force paper kite settings
        # without requiring the smoke flag (checklist itself is the intentional runner).
        smoke_env = {**stripped, "ZERODHA_SMOKE": stripped.get("ZERODHA_SMOKE") or "1"}
        config = kite_market_smoke_config(smoke_env)
    except GrowConfigError as exc:
        reason = str(exc)
        if _is_auth_block(reason):
            return _all_blocked(reason, "kite_market_smoke_config")
        return _all_failed(reason, "kite_market_smoke_config")
    except GrowLiveTradingDisabled as exc:
        return _all_failed(str(exc), "inspect_environment")

    if config.live_data.live_trading or config.execution.live_trading_enabled:
        return _all_failed("LIVE_EXECUTION_FORBIDDEN", "kite_market_smoke_config")
    if not config.live_data.paper_mode:
        return _all_failed("paper_mode must be true", "kite_market_smoke_config")

    provider: KiteMarketProvider | None = None
    try:
        provider = open_provider(api_key, access_token, proof_clock)
    except GrowConfigError as exc:
        reason = str(exc)
        if _is_auth_block(reason):
            return _all_blocked(reason, "KiteMarketProvider.connect")
        return _all_failed(reason, "KiteMarketProvider.connect")
    except Exception as exc:  # noqa: BLE001
        return _all_failed(_public_reason(exc), "KiteMarketProvider.connect")

    assert provider is not None
    calendar = SessionCalendar(config.market, clock=provider.clock)
    staleness = int(config.live_data.max_staleness_seconds)
    try:
        quotes = _poll_quotes(
            provider,
            attempts=hooks.poll_attempts,
            max_staleness_seconds=staleness,
            calendar=calendar,
        )
        # Ensure CE+PE opportunity for the selected contracts.
        need_more = (
            _find_selected_quote(quotes, selected=provider.selected, option_type="CE") is None
            or _find_selected_quote(quotes, selected=provider.selected_put, option_type="PE") is None
        )
        if need_more:
            more = _poll_quotes(
                provider,
                attempts=hooks.poll_attempts,
                max_staleness_seconds=staleness,
                calendar=calendar,
            )
            seen = {(q.get("option_type"), q.get("provider_symbol_id"), q.get("provider_symbol")) for q in quotes}
            for row in more:
                key = (row.get("option_type"), row.get("provider_symbol_id"), row.get("provider_symbol"))
                if key not in seen:
                    quotes.append(row)
                    seen.add(key)

        ce = _prove_side_tick(
            item_id="real_zerodha_ce_tick",
            option_type="CE",
            provider=provider,
            quotes=quotes,
        )
        pe = _prove_side_tick(
            item_id="real_zerodha_pe_tick",
            option_type="PE",
            provider=provider,
            quotes=quotes,
        )
        chain = _prove_option_chain(provider)
        # Reconnect last so CE/PE/chain use the initial healthy session.
        reconnect = _prove_websocket_reconnect(
            provider,
            attempts=hooks.poll_attempts,
            max_staleness_seconds=staleness,
            reconnect_wait_seconds=hooks.reconnect_wait_seconds,
            calendar=calendar,
            pre_reconnect_quotes=quotes,
        )
        return (ce, pe, reconnect, chain)
    finally:
        try:
            provider.disconnect()
        except Exception:
            pass


__all__ = [
    "LIVE_PROOF_IDS",
    "LiveProofHooks",
    "LiveProofResult",
    "assert_live_proofs_forbid_broker_orders",
    "run_zerodha_live_proofs",
]
