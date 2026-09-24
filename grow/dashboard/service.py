"""Read-only dashboard views.

Consumes existing config, PaperBook snapshots, safety lock state, optional
replay/cycle artifacts, and Zerodha market-data auth status.
Does not mint RiskStamps, place fills, or talk to brokers.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from grow.campaign.config import campaign_paper_config
from grow.clock import IST, SystemClock
from grow.config import GrowConfig, load_config
from grow.execution.lock import LIVE_TRADING_COMPILED, scrub_broker_credentials_for_paper
from grow.market.session import SessionCalendar
from grow.options.select import session_day
from grow.paper.checkpoint import load_paper_checkpoint
from grow.paper.ledger import PaperBook, Position
from grow.types import Symbol

# Short TTLs: verified auth / live quotes refresh without hammering Kite.
_ZERODHA_AUTH_TTL_OK_S = 30.0
_ZERODHA_AUTH_TTL_FAIL_S = 10.0
_LIVE_QUOTE_TTL_OK_S = 15.0
_LIVE_QUOTE_TTL_FAIL_S = 8.0
_LIVE_CHAIN_TTL_OK_S = 20.0
_LIVE_CHAIN_TTL_FAIL_S = 8.0
_CHAIN_STRIKE_WINDOW = 5  # display ±N strikes around ATM from instrument dump


def _lock_status() -> dict[str, Any]:
    # Lazy import avoids circular import with grow.dashboard.__init__.
    from grow.dashboard import lock_status

    return lock_status()


def _snapshot(config: GrowConfig, book: PaperBook, last_cycle: Any | None = None) -> dict[str, Any]:
    from grow.dashboard import snapshot

    return snapshot(config, book, last_cycle)


def align_dashboard_config(config: GrowConfig) -> GrowConfig:
    """Use the same capital/risk profile as CampaignRunner / RiskGuard paper path.

    ``GROW_STARTING_CASH`` (and similar env overlays) can otherwise leave the
    dashboard on a larger book while the campaign profile is ₹10K.
    """
    if config.paper.capital_profile:
        return campaign_paper_config(config)
    return config


_NOT_AVAILABLE = "NOT AVAILABLE"

_SECRET_KEY_RE = re.compile(
    r"(secret|password|token|api[_-]?key|access[_-]?token|authorization|credential|kite)",
    re.IGNORECASE,
)

_SECRET_ENV_NAMES = frozenset(
    {
        "GROW_RISK_SECRET",
        "KITE_API_KEY",
        "KITE_API_SECRET",
        "KITE_ACCESS_TOKEN",
        "OPENAI_API_KEY",
        "XAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GROW_HISTORICAL_API_KEY",
        "UPSTOX_ACCESS_TOKEN",
        "DHAN_ACCESS_TOKEN",
        "BROKER_API_KEY",
    }
)


def redact_secrets(value: Any) -> Any:
    """Recursively strip secret-looking keys from API payloads."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if key_s.upper() in _SECRET_ENV_NAMES or _SECRET_KEY_RE.search(key_s):
                continue
            out[key_s] = redact_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    return value


def _empty_book(config: GrowConfig) -> PaperBook:
    return PaperBook(cash=config.paper.starting_cash, currency=config.market.currency)


def _book_from_checkpoint_ledger(config: GrowConfig, ledger: Mapping[str, Any]) -> PaperBook:
    book = PaperBook(
        cash=float(ledger.get("cash", config.paper.starting_cash)),
        currency=config.market.currency,
        realized_pnl=float(ledger.get("realized_pnl", 0.0)),
    )
    positions: dict[str, Position] = {}
    for row in ledger.get("positions") or []:
        ticker = str(row["ticker"])
        positions[ticker] = Position(
            symbol=Symbol(ticker),
            quantity=int(row["quantity"]),
            average_price=float(row["average_price"]),
        )
    book.positions = positions
    return book


class DashboardService:
    """In-process read model for the paper dashboard (read-only)."""

    def __init__(
        self,
        config: GrowConfig | None = None,
        *,
        book: PaperBook | None = None,
        last_cycle: Any | None = None,
        checkpoint_path: Path | str | None = None,
        option_positions: list[dict[str, Any]] | None = None,
        replay_root: Path | str | None = None,
        market_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        raw = config if config is not None else load_config()
        self.config = align_dashboard_config(raw)
        self.config.assert_safe()
        self.checkpoint_path = self._resolve_checkpoint_path(checkpoint_path)
        self.replay_root = self._resolve_replay_root(replay_root)
        self.last_cycle = last_cycle
        self._engine_positions: list[dict[str, Any]] = list(option_positions or [])
        self._checkpoint_meta: dict[str, Any] | None = None
        self._last_risk_daily_pnl: float | None = None
        self._replay_record: dict[str, Any] | None = None
        self._market_snapshot: dict[str, Any] | None = (
            dict(market_snapshot) if market_snapshot is not None else None
        )
        # (expires_at_monotonic, payload) — failures use short TTL so they can retry.
        self._zerodha_cache: tuple[float, dict[str, Any]] | None = None
        self._nifty_cache: tuple[float, dict[str, Any]] | None = None
        self._chain_cache: tuple[float, dict[str, Any]] | None = None
        self.book = book if book is not None else _empty_book(self.config)
        if book is None:
            self._try_load_checkpoint()
        if self.last_cycle is None:
            self._try_load_cycle_artifacts()

    @staticmethod
    def _resolve_checkpoint_path(explicit: Path | str | None) -> Path | None:
        if explicit is not None:
            return Path(explicit)
        env = (os.environ.get("GROW_DASHBOARD_CHECKPOINT") or "").strip()
        if env:
            return Path(env)
        default = Path.cwd() / "results" / "paper_checkpoint.json"
        return default if default.is_file() else None

    @staticmethod
    def _resolve_replay_root(explicit: Path | str | None) -> Path | None:
        if explicit is not None:
            return Path(explicit)
        env = (os.environ.get("GROW_DASHBOARD_REPLAY") or "").strip()
        if env:
            return Path(env)
        default = Path.cwd() / "results" / "trade_replay"
        return default if default.is_dir() else None

    def _try_load_cycle_artifacts(self) -> None:
        """Load last cycle / replay JSON if present (read-only artifacts)."""
        cycle_path = (os.environ.get("GROW_DASHBOARD_LAST_CYCLE") or "").strip()
        if cycle_path:
            path = Path(cycle_path)
            if path.is_file():
                try:
                    self.last_cycle = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
        snap_path = (os.environ.get("GROW_DASHBOARD_MARKET_SNAPSHOT") or "").strip()
        if snap_path:
            path = Path(snap_path)
            if path.is_file():
                try:
                    self._market_snapshot = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
        if self.replay_root is None or not self.replay_root.is_dir():
            return
        latest: dict[str, Any] | None = None
        latest_mtime = -1.0
        for path in self.replay_root.glob("*.json"):
            if path.name.startswith("."):
                continue
            try:
                mtime = path.stat().st_mtime
                if mtime < latest_mtime:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            latest = payload
            latest_mtime = mtime
        if latest is not None:
            self._replay_record = latest
            if self.last_cycle is None:
                # Prefer full cycle-shaped payload when present; else wrap decision.
                if "decision" in latest and "cycle_id" in latest:
                    self.last_cycle = {
                        "snapshot_id": latest.get("snapshot_id"),
                        "cycle_id": latest.get("cycle_id"),
                        "decision": latest.get("decision"),
                        "execution": latest.get("paper_fill"),
                        "package_digest": latest.get("package_digest"),
                        "runner_version": "campaign.replay.v1",
                        "paper_mode": True,
                        "live_trading": False,
                        "broker_order_path": False,
                    }
            if self._market_snapshot is None and isinstance(latest.get("snapshot"), dict):
                self._market_snapshot = dict(latest["snapshot"])

    def _try_load_checkpoint(self) -> None:
        path = self.checkpoint_path
        if path is None or not path.is_file():
            return
        try:
            payload = load_paper_checkpoint(path)
        except Exception:
            self._checkpoint_meta = {
                "path": str(path),
                "loaded": False,
                "status": "Not available",
                "note": "Checkpoint present but could not be loaded safely.",
            }
            return
        engine = payload.get("engine") or {}
        ledger = engine.get("ledger") or {}
        self.book = _book_from_checkpoint_ledger(self.config, ledger)
        raw_positions = engine.get("positions") or []
        self._engine_positions = [dict(row) for row in raw_positions if isinstance(row, Mapping)]
        raw_daily = engine.get("last_risk_daily_pnl")
        self._last_risk_daily_pnl = None if raw_daily is None else float(raw_daily)
        self._checkpoint_meta = {
            "path": str(path),
            "loaded": True,
            "session_id": engine.get("session_id"),
            "paper_mode": payload.get("paper_mode", True),
            "live_trading": payload.get("live_trading", False),
            "broker_order_path": payload.get("broker_order_path", False),
            "data_label": "FIXTURE / PAPER DATA",
            "last_risk_daily_pnl": self._last_risk_daily_pnl,
        }

    def safety(self) -> dict[str, Any]:
        lock = _lock_status()
        return {
            "paper_trading": True,
            "live_trading_disabled": True,
            "broker_orders_disabled": True,
            "live_trading_compiled": bool(LIVE_TRADING_COMPILED),
            "execution_mode": self.config.execution.mode,
            "live_trading_enabled": bool(self.config.execution.live_trading_enabled),
            "broker_order_path": False,
            "venue_id": self.config.paper.venue_id,
            "lock": lock,
            "banners": [
                "PAPER TRADING",
                "LIVE TRADING DISABLED",
                "BROKER ORDERS DISABLED",
                "READ ONLY UI",
            ],
            "can_enable_live_trading": False,
            "ui_mode": "read_only",
            "canonical_paper_path": "campaign",
            "note": (
                "Read-only paper console. No buy/sell controls. "
                "Canonical path: Analysis → CampaignOptions → Decision → CEO gate → RiskGuard → paper."
            ),
        }

    def _cache_get(self, entry: tuple[float, dict[str, Any]] | None) -> dict[str, Any] | None:
        if entry is None:
            return None
        expires_at, payload = entry
        if time.monotonic() >= expires_at:
            return None
        return dict(payload)

    def _cache_put(
        self,
        payload: Mapping[str, Any],
        *,
        ok: bool,
        ttl_ok: float,
        ttl_fail: float,
    ) -> tuple[float, dict[str, Any]]:
        ttl = ttl_ok if ok else ttl_fail
        return (time.monotonic() + ttl, dict(payload))

    def _zerodha_public(self, *, probe: bool = True, force: bool = False) -> dict[str, Any]:
        """Zerodha status for dashboard surfaces.

        When ``probe`` is True (default for market/system), run a read-only
        profile check with a short TTL so CONNECTED means verified.
        """
        from grow.dashboard.zerodha_auth import auth_status

        if not probe:
            return auth_status(probe=False).to_public_dict()

        if not force:
            cached = self._cache_get(self._zerodha_cache)
            if cached is not None:
                return cached

        status = auth_status(probe=True).to_public_dict()
        connected = str(status.get("status") or "").upper() == "CONNECTED"
        self._zerodha_cache = self._cache_put(
            status,
            ok=connected,
            ttl_ok=_ZERODHA_AUTH_TTL_OK_S,
            ttl_fail=_ZERODHA_AUTH_TTL_FAIL_S,
        )
        return dict(status)

    def _live_kite_transport(self):
        """Existing read-only Kite transport (quotes only; no orders)."""
        from grow.live_data.kite_market import RealKiteTransport, load_kite_market_secrets

        api_key, access_token = load_kite_market_secrets(dict(os.environ))
        return RealKiteTransport(api_key=api_key, access_token=access_token)

    def _probe_nifty_ltp(self, *, force: bool = False) -> dict[str, Any]:
        """LIVE index LTP via existing Kite HTTP quote path (no websocket required)."""
        if not force:
            cached = self._cache_get(self._nifty_cache)
            if cached is not None:
                return cached

        result: dict[str, Any] = {
            "price": _NOT_AVAILABLE,
            "timestamp": _NOT_AVAILABLE,
            "provenance": _NOT_AVAILABLE,
            "freshness": _NOT_AVAILABLE,
            "quote_age_seconds": _NOT_AVAILABLE,
            "reason": "Zerodha market-data session not available",
            "ok": False,
        }
        try:
            transport = self._live_kite_transport()
            # HTTP quote only — avoid websocket connect (quotes do not need it).
            price, _token = transport.fetch_index_quote("NIFTY")
            now = SystemClock().now().astimezone(IST)
            result = {
                "price": float(price),
                "timestamp": now.isoformat(),
                "provenance": "LIVE",
                "freshness": "REST_QUOTE",
                "quote_age_seconds": 0.0,
                "reason": None,
                "ok": True,
            }
        except Exception as exc:
            # Prefer GrowConfigError code text when present; never include secrets.
            detail = str(exc).strip() or type(exc).__name__
            if len(detail) > 120:
                detail = type(exc).__name__
            result["reason"] = detail
            result["ok"] = False

        self._nifty_cache = self._cache_put(
            result,
            ok=bool(result.get("ok")),
            ttl_ok=_LIVE_QUOTE_TTL_OK_S,
            ttl_fail=_LIVE_QUOTE_TTL_FAIL_S,
        )
        return dict(result)

    def _fetch_live_option_chain(self, *, spot: float, force: bool = False) -> dict[str, Any]:
        """Read-only option quotes via existing Kite instruments + quote APIs."""
        if not force:
            cached = self._cache_get(self._chain_cache)
            if cached is not None:
                return cached

        empty: dict[str, Any] = {
            "available": False,
            "message": _NOT_AVAILABLE,
            "label": "Live option chain unavailable",
            "data_label": _NOT_AVAILABLE,
            "provenance": _NOT_AVAILABLE,
            "freshness": _NOT_AVAILABLE,
            "rows": [],
            "selected": None,
            "ce": _NOT_AVAILABLE,
            "strike": _NOT_AVAILABLE,
            "pe": _NOT_AVAILABLE,
            "reason": None,
            "ok": False,
        }
        try:
            from grow.live_data.expiry_class import ExpiryClassifier
            from grow.live_data.kite_market import (
                QUOTE_URL,
                decode_http_text,
                parse_nfo_instruments,
                select_option_pair,
            )

            transport = self._live_kite_transport()
            now = SystemClock().now().astimezone(IST)
            as_of = now.date()
            catalog = parse_nfo_instruments(transport.fetch_instruments())
            nifty_rows = [row for row in catalog if row.underlying == "NIFTY"]
            if not nifty_rows:
                empty["reason"] = "METADATA_UNAVAILABLE"
                empty["message"] = "NOT AVAILABLE — no NIFTY option instruments"
                self._chain_cache = self._cache_put(
                    empty, ok=False, ttl_ok=_LIVE_CHAIN_TTL_OK_S, ttl_fail=_LIVE_CHAIN_TTL_FAIL_S
                )
                return dict(empty)

            classifier = ExpiryClassifier()
            # Policy-preferred expiry (NIFTY: WEEKLY_PREFERRED), then ATM CE/PE.
            atm_ce, atm_pe = select_option_pair(
                nifty_rows,
                spots={"NIFTY": float(spot)},
                as_of=as_of,
                classifier=classifier,
            )
            expiry = atm_ce.expiry
            pool = [row for row in nifty_rows if row.expiry == expiry]
            strikes = sorted({float(row.strike) for row in pool})
            atm = float(atm_ce.strike)
            # Display-only window around ATM (not campaign scoring / selection policy).
            ordered = sorted(strikes, key=lambda s: (abs(s - atm), s))
            window = sorted(ordered[: max(1, _CHAIN_STRIKE_WINDOW * 2 + 1)])

            by_strike: dict[float, dict[str, Any]] = {}
            query_parts: list[str] = []
            for strike in window:
                by_strike[strike] = {
                    "underlying": "NIFTY",
                    "expiry": expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry),
                    "strike": strike,
                    "dte": (expiry - as_of).days if isinstance(expiry, date) else _NOT_AVAILABLE,
                    "ce_ltp": _NOT_AVAILABLE,
                    "ce_bid": _NOT_AVAILABLE,
                    "ce_ask": _NOT_AVAILABLE,
                    "pe_ltp": _NOT_AVAILABLE,
                    "pe_bid": _NOT_AVAILABLE,
                    "pe_ask": _NOT_AVAILABLE,
                    "score": _NOT_AVAILABLE,
                    "eligibility": "LIVE_QUOTE",
                    "rejection_reason": _NOT_AVAILABLE,
                }
                for side in ("CE", "PE"):
                    match = next(
                        (
                            row
                            for row in pool
                            if float(row.strike) == strike and row.option_type == side
                        ),
                        None,
                    )
                    if match is not None:
                        query_parts.append(f"i={urllib.parse.quote('NFO:' + match.tradingsymbol)}")

            if not query_parts:
                empty["reason"] = "NO_VALID_OPTION"
                empty["message"] = "NOT AVAILABLE — no quotable CE/PE near ATM"
                self._chain_cache = self._cache_put(
                    empty, ok=False, ttl_ok=_LIVE_CHAIN_TTL_OK_S, ttl_fail=_LIVE_CHAIN_TTL_FAIL_S
                )
                return dict(empty)

            url = f"{QUOTE_URL}?{'&'.join(query_parts)}"
            body = json.loads(decode_http_text(transport._get(url)))
            data = body.get("data") or {}
            if not isinstance(data, Mapping) or not data:
                empty["reason"] = "METADATA_UNAVAILABLE"
                empty["message"] = "NOT AVAILABLE — option quote response empty"
                self._chain_cache = self._cache_put(
                    empty, ok=False, ttl_ok=_LIVE_CHAIN_TTL_OK_S, ttl_fail=_LIVE_CHAIN_TTL_FAIL_S
                )
                return dict(empty)

            for key, row in data.items():
                if not isinstance(row, Mapping):
                    continue
                # key like NFO:NIFTY25SEP23250CE
                sym = str(key).split(":", 1)[-1]
                match = next((r for r in pool if r.tradingsymbol == sym), None)
                if match is None:
                    continue
                strike = float(match.strike)
                bucket = by_strike.get(strike)
                if bucket is None:
                    continue
                depth = row.get("depth") or {}
                buy = (depth.get("buy") or [{}])
                sell = (depth.get("sell") or [{}])
                bid = (buy[0] or {}).get("price") if buy else None
                ask = (sell[0] or {}).get("price") if sell else None
                ltp = row.get("last_price")
                prefix = "ce" if match.option_type == "CE" else "pe"
                bucket[f"{prefix}_ltp"] = ltp if ltp is not None else _NOT_AVAILABLE
                bucket[f"{prefix}_bid"] = bid if bid is not None else _NOT_AVAILABLE
                bucket[f"{prefix}_ask"] = ask if ask is not None else _NOT_AVAILABLE
                # Optional greeks when provider includes them.
                for g in ("delta", "gamma", "theta", "vega", "iv", "implied_volatility"):
                    if row.get(g) is not None:
                        bucket[g if g != "implied_volatility" else "iv"] = row.get(g)

            rows = [by_strike[s] for s in window if s in by_strike]
            selected = {
                "strike": float(atm_ce.strike),
                "expiry": atm_ce.expiry.isoformat()
                if hasattr(atm_ce.expiry, "isoformat")
                else str(atm_ce.expiry),
                "option_type": "CE",
                "instrument": atm_ce.tradingsymbol,
            }
            payload = {
                "available": bool(rows),
                "message": None if rows else _NOT_AVAILABLE,
                "label": f"{len(rows)} strike(s) from live Kite quotes",
                "data_label": "LIVE",
                "provenance": "LIVE",
                "freshness": "REST_QUOTE",
                "snapshot_id": _NOT_AVAILABLE,
                "rows": rows,
                "selected": selected,
                "ce": "CE",
                "strike": float(atm_ce.strike),
                "pe": "PE",
                "reason": None,
                "ok": bool(rows),
            }
            self._chain_cache = self._cache_put(
                payload, ok=True, ttl_ok=_LIVE_CHAIN_TTL_OK_S, ttl_fail=_LIVE_CHAIN_TTL_FAIL_S
            )
            return dict(payload)
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            if len(detail) > 120:
                detail = type(exc).__name__
            empty["reason"] = detail
            empty["message"] = f"NOT AVAILABLE — {detail}"
            self._chain_cache = self._cache_put(
                empty, ok=False, ttl_ok=_LIVE_CHAIN_TTL_OK_S, ttl_fail=_LIVE_CHAIN_TTL_FAIL_S
            )
            return dict(empty)

    def market_status(self) -> dict[str, Any]:
        calendar = SessionCalendar(self.config.market)
        state = calendar.state()
        zerodha = self._zerodha_public(probe=True)
        connected = str(zerodha.get("status") or "").upper() == "CONNECTED"
        token_present = bool(zerodha.get("access_token_present"))
        nifty = {"price": _NOT_AVAILABLE, "label": _NOT_AVAILABLE, "timestamp": _NOT_AVAILABLE}
        provenance: Any = _NOT_AVAILABLE
        freshness: Any = _NOT_AVAILABLE
        quote_age: Any = _NOT_AVAILABLE
        note_parts: list[str] = []
        data_status = "NOT AVAILABLE"

        snap = self._market_snapshot
        snap_provenance = _NOT_AVAILABLE
        if isinstance(snap, Mapping):
            snap_provenance = str(
                snap.get("market_data_source")
                or (snap.get("diagnostics") or {}).get("market_data_source")
                or _NOT_AVAILABLE
            )

        # Live quotes when Zerodha session is verified CONNECTED.
        live_ok = False
        if connected:
            probed = self._probe_nifty_ltp()
            if probed.get("ok") and probed.get("price") is not _NOT_AVAILABLE:
                live_ok = True
                nifty = {
                    "price": probed["price"],
                    "label": "LIVE",
                    "timestamp": probed.get("timestamp") or _NOT_AVAILABLE,
                }
                provenance = "LIVE"
                freshness = probed.get("freshness") or "REST_QUOTE"
                quote_age = probed.get("quote_age_seconds", 0.0)
                data_status = "LIVE"
            else:
                reason = probed.get("reason") or "quote unavailable"
                note_parts.append(f"NIFTY live quote: {reason}")
                data_status = "NOT AVAILABLE"
        elif token_present:
            note_parts.append(
                "Access token present but Zerodha session not verified (DISCONNECTED)."
            )
            data_status = "NOT AVAILABLE"
        else:
            note_parts.append(
                "Zerodha market-data not connected. Use Settings → Connect (quotes only; no orders)."
            )
            data_status = "NOT CONNECTED"

        # Snapshot provenance only when live is unavailable — never label fixture as LIVE.
        if not live_ok and isinstance(snap, Mapping):
            provenance = snap_provenance
            under = (snap.get("underlyings") or {}).get("NIFTY") or {}
            if isinstance(under, Mapping) and nifty["price"] is _NOT_AVAILABLE:
                price = under.get("ltp") if under.get("ltp") is not None else under.get("spot")
                if price is not None:
                    nifty = {
                        "price": price,
                        "label": provenance,
                        "timestamp": under.get("quote_timestamp")
                        or snap.get("decision_timestamp")
                        or _NOT_AVAILABLE,
                    }
                    age = under.get("quote_age_seconds")
                    if age is not None:
                        quote_age = age
                    quality = snap.get("data_quality")
                    freshness = str(quality) if quality is not None else freshness
                    if provenance == "FIXTURE":
                        data_status = "FIXTURE"
                    elif provenance == "MIXED":
                        data_status = "NOT AVAILABLE"
                    elif data_status == "NOT AVAILABLE" and provenance not in {_NOT_AVAILABLE, ""}:
                        data_status = str(provenance)
            notes = snap.get("quality_notes") or ()
            if notes:
                note_parts.append(", ".join(str(n) for n in notes[:6]))
            if str(snap.get("data_quality") or "").upper() in {"STALE", "INSUFFICIENT", "REJECTED"}:
                note_parts.append(
                    f"Snapshot data quality: {snap.get('data_quality')} (fail-closed)."
                )

        if provenance == "MIXED":
            note_parts.append("MIXED provenance is rejected for paper campaign trading.")
            data_status = "NOT AVAILABLE"

        return {
            "exchange": self.config.market.exchange,
            "session_state": state.value,
            "session_open": self.config.market.session_open,
            "session_close": self.config.market.session_close,
            "square_off": self.config.market.square_off,
            "timezone": self.config.timezone,
            "market_data": data_status,
            "market_data_connected": connected and live_ok,
            "zerodha_status": zerodha.get("status"),
            "zerodha": zerodha,
            "data_provider": self.config.data.provider,
            "data_label": provenance,
            "provenance": provenance,
            "freshness": freshness,
            "quote_age_seconds": quote_age,
            "indices": {
                "NIFTY": nifty,
                "BANKNIFTY": {
                    "price": _NOT_AVAILABLE,
                    "label": _NOT_AVAILABLE,
                    "timestamp": _NOT_AVAILABLE,
                },
            },
            "note": " · ".join(note_parts)
            if note_parts
            else (
                f"Session {state.value}. "
                + (
                    "Live Zerodha market-data quotes."
                    if live_ok
                    else "Awaiting verified Zerodha market-data."
                )
            ),
        }

    def config_view(self) -> dict[str, Any]:
        cfg = self.config
        return redact_secrets(
            {
                "version": cfg.version,
                "timezone": cfg.timezone,
                "source_path": cfg.source_path,
                "execution": {
                    "mode": cfg.execution.mode,
                    "live_trading_enabled": cfg.execution.live_trading_enabled,
                },
                "market": {
                    "exchange": cfg.market.exchange,
                    "currency": cfg.market.currency,
                    "session_open": cfg.market.session_open,
                    "session_close": cfg.market.session_close,
                    "square_off": cfg.market.square_off,
                    "product": cfg.market.product,
                    "universe": list(cfg.market.universe),
                },
                "risk": {
                    "max_position_notional": cfg.risk.max_position_notional,
                    "max_gross_notional": cfg.risk.max_gross_notional,
                    "max_daily_loss": cfg.risk.max_daily_loss,
                    "require_stop_loss": cfg.risk.require_stop_loss,
                    "max_symbol_concentration": cfg.risk.max_symbol_concentration,
                    "ruleset": cfg.risk.ruleset,
                    "allow_short": cfg.risk.allow_short,
                    "concentration_basis": cfg.risk.concentration_basis,
                    "max_open_positions": cfg.risk.max_open_positions,
                    "max_per_trade_risk": cfg.risk.max_per_trade_risk,
                },
                "paper": {
                    "starting_cash": cfg.paper.starting_cash,
                    "venue_id": cfg.paper.venue_id,
                    "fill_model": cfg.paper.fill_model,
                    "capital_profile": cfg.paper.capital_profile,
                    "price_mode": cfg.paper.price_mode,
                },
                "model": {
                    "provider": cfg.model.provider,
                    "deep_model": cfg.model.deep_model,
                    "fast_model": cfg.model.fast_model,
                },
                "data": {
                    "provider": cfg.data.provider,
                    "allow_live_feed": cfg.data.allow_live_feed,
                },
                "live_data": {
                    "enabled": cfg.live_data.enabled,
                    "provider": cfg.live_data.provider,
                    "paper_mode": cfg.live_data.paper_mode,
                    "live_trading": cfg.live_data.live_trading,
                },
                "strategies": {
                    "universe": list(cfg.strategies.universe),
                    "primary_timeframe": cfg.strategies.primary_timeframe,
                },
                "options": {
                    "enabled": cfg.options.enabled,
                    "provider": cfg.options.provider,
                    "allow_live_chain": cfg.options.allow_live_chain,
                },
            }
        )

    def positions_view(self) -> dict[str, Any]:
        book_snap = self.book.snapshot()
        cash_positions = []
        for pos in (book_snap.get("positions") or {}).values():
            cash_positions.append(
                {
                    "symbol": pos.get("ticker"),
                    "contract": pos.get("ticker"),
                    "quantity": pos.get("quantity"),
                    "entry_price": pos.get("average_price"),
                    "current_price": _NOT_AVAILABLE,
                    "pnl": _NOT_AVAILABLE,
                    "notional": pos.get("notional"),
                    "status": "OPEN",
                    "source": "paper_book",
                    "data_label": "FIXTURE / PAPER DATA",
                }
            )

        option_rows = []
        for row in self._engine_positions:
            if str(row.get("state", "OPEN")).upper() not in {"OPEN", "EXIT_PENDING", "HALTED"}:
                continue
            option_rows.append(
                {
                    "symbol": row.get("underlying"),
                    "contract": row.get("contract_id"),
                    "quantity": row.get("quantity"),
                    "entry_price": row.get("entry_price"),
                    "current_price": row.get("current_price")
                    if row.get("current_price") is not None
                    else _NOT_AVAILABLE,
                    "pnl": row.get("unrealized_pnl")
                    if row.get("unrealized_pnl") is not None
                    else _NOT_AVAILABLE,
                    "status": row.get("state"),
                    "strike": row.get("strike"),
                    "option_type": row.get("option_type"),
                    "expiry": row.get("expiry"),
                    "source": "paper_checkpoint",
                    "data_label": "FIXTURE / PAPER DATA",
                }
            )

        open_rows = option_rows or cash_positions
        return redact_secrets(
            {
                "open_count": len(open_rows),
                "positions": open_rows,
                "message": None if open_rows else "No open paper positions",
                "book": book_snap,
                "checkpoint": self._checkpoint_meta,
            }
        )

    def risk_view(self) -> dict[str, Any]:
        risk = self.config.risk
        book = self.book
        daily_pnl = book.snapshot().get("pnl", {}).get("true_daily_pnl")
        if daily_pnl is None and self._last_risk_daily_pnl is not None:
            # Prefer RiskGuard trading-day figure from paper checkpoint when present.
            daily_pnl = self._last_risk_daily_pnl
        realized = book.realized_pnl
        remaining: Any
        if daily_pnl is None:
            # Documented limitation: true daily P&L is not available yet.
            remaining = _NOT_AVAILABLE
            today_pnl: Any = _NOT_AVAILABLE
        else:
            today_pnl = daily_pnl
            # remaining daily risk = max daily loss + current daily P&L
            remaining = round(float(risk.max_daily_loss) + float(daily_pnl), 2)

        open_count = len(self.positions_view()["positions"])
        return redact_secrets(
            {
                "status": "ACTIVE",
                "status_label": "Risk Guard ACTIVE",
                "ruleset": risk.ruleset,
                "paper_capital": self.config.paper.starting_cash,
                "available_capital": book.cash,
                "cash": book.cash,
                "currency": book.currency,
                "max_daily_loss": risk.max_daily_loss,
                "max_per_trade_risk": risk.max_per_trade_risk
                if risk.max_per_trade_risk is not None
                else _NOT_AVAILABLE,
                "max_open_positions": risk.max_open_positions
                if risk.max_open_positions is not None
                else _NOT_AVAILABLE,
                "max_position_notional": risk.max_position_notional,
                "max_gross_notional": risk.max_gross_notional,
                "starting_capital": self.config.paper.starting_cash,
                "current_exposure": book.gross_notional,
                "open_positions": open_count,
                "max_positions": risk.max_open_positions
                if risk.max_open_positions is not None
                else _NOT_AVAILABLE,
                "today_pnl": today_pnl,
                "daily_pnl": today_pnl,
                "realized_pnl_at_cost": realized,
                "remaining_daily_risk": remaining,
                "require_stop_loss": risk.require_stop_loss,
                "allow_short": risk.allow_short,
                "capital_profile": self.config.paper.capital_profile,
                "read_only": True,
                "note": (
                    "Limits from effective GrowConfig (same profile as RiskGuard / campaign). "
                    f"True daily P&L may be {_NOT_AVAILABLE} until valuation exists."
                ),
            }
        )

    def _decision_payload(self) -> dict[str, Any] | None:
        """Extract IntegratedDecision-shaped dict from cycle / replay artifacts."""
        cycle = self.last_cycle
        if cycle is not None:
            payload = cycle.to_dict() if hasattr(cycle, "to_dict") else dict(cycle)
            decision = payload.get("decision")
            if isinstance(decision, Mapping):
                return dict(decision)
            # Flattened CampaignCycleResult fields without nested decision.
            if payload.get("decision_action") is not None or payload.get("action") is not None:
                return {
                    "analysis_cycle_id": payload.get("cycle_id") or payload.get("analysis_cycle_id"),
                    "snapshot_id": payload.get("snapshot_id"),
                    "status": payload.get("decision_status") or payload.get("status"),
                    "action": payload.get("decision_action") or payload.get("action"),
                    "campaign_signal": payload.get("campaign_signal"),
                    "reason_codes": payload.get("reason_codes") or (),
                    "risk_guard_result": payload.get("risk_guard_result"),
                    "risk_guard_reason": payload.get("risk_guard_reason"),
                    "trade_candidate": payload.get("trade_candidate"),
                    "data_quality_status": payload.get("data_quality_status"),
                    "gate_results": payload.get("gate_results") or (),
                }
        replay = self._replay_record
        if isinstance(replay, Mapping) and isinstance(replay.get("decision"), Mapping):
            return dict(replay["decision"])
        return None

    def _stale_no_trade_reason(self) -> str | None:
        snap = self._market_snapshot
        if not isinstance(snap, Mapping):
            return None
        quality = str(snap.get("data_quality") or "").upper()
        if quality in {"STALE", "INSUFFICIENT", "REJECTED"}:
            notes = snap.get("quality_notes") or ()
            detail = ", ".join(str(n) for n in notes[:4]) if notes else quality
            return f"NO TRADE — market data {quality}: {detail}"
        source = str(snap.get("market_data_source") or "").upper()
        if source == "MIXED":
            return "NO TRADE — MIXED market-data provenance rejected"
        return None

    def agents_view(self) -> dict[str, Any]:
        decision = self._decision_payload()
        stale_reason = self._stale_no_trade_reason()
        base_meta = {
            "model_provider": self.config.model.provider,
            "tradingagents_enabled": self.config.tradingagents.enabled,
        }
        if decision is None:
            message = stale_reason or "AI decision data not available"
            final_action = "NO TRADE" if stale_reason else _NOT_AVAILABLE
            return redact_secrets(
                {
                    "available": bool(stale_reason),
                    "message": message,
                    **base_meta,
                    "decisions": [
                        {
                            "agent": "DecisionEngine",
                            "cycle_id": _NOT_AVAILABLE,
                            "snapshot_id": (
                                (self._market_snapshot or {}).get("snapshot_id")
                                if self._market_snapshot
                                else _NOT_AVAILABLE
                            ),
                            "candidate": _NOT_AVAILABLE,
                            "option_type": _NOT_AVAILABLE,
                            "ce_pe": _NOT_AVAILABLE,
                            "strike": _NOT_AVAILABLE,
                            "expiry": _NOT_AVAILABLE,
                            "dte": _NOT_AVAILABLE,
                            "score": _NOT_AVAILABLE,
                            "ceo_gate": _NOT_AVAILABLE,
                            "risk_guard": _NOT_AVAILABLE,
                            "final_action": final_action,
                            "decision": final_action,
                            "signal": final_action,
                            "confidence": _NOT_AVAILABLE,
                            "timestamp": _NOT_AVAILABLE,
                            "reason": message,
                            "risk_verdict": _NOT_AVAILABLE,
                            "risk_reason": message if stale_reason else _NOT_AVAILABLE,
                            "provenance": (
                                (self._market_snapshot or {}).get("market_data_source")
                                if self._market_snapshot
                                else _NOT_AVAILABLE
                            ),
                            "symbol": _NOT_AVAILABLE,
                        }
                    ]
                    if stale_reason
                    else [],
                    "last_cycle": None
                    if self.last_cycle is None
                    else (
                        self.last_cycle.to_dict()
                        if hasattr(self.last_cycle, "to_dict")
                        else dict(self.last_cycle)
                    ),
                }
            )

        candidate = decision.get("trade_candidate") or {}
        signal = decision.get("campaign_signal") or {}
        reasons = list(decision.get("reason_codes") or [])
        action = str(decision.get("action") or "NO_TRADE").upper()
        status = str(decision.get("status") or "").upper()
        if action in {"NO_TRADE", "NO TRADE"} or status in {"NO_TRADE", "BLOCKED"}:
            final_action = "NO TRADE"
        else:
            final_action = action

        gate_rows = decision.get("gate_results") or []
        ceo_gate: Any = _NOT_AVAILABLE
        for row in gate_rows:
            if not isinstance(row, Mapping):
                continue
            gate_name = str(row.get("gate") or "").lower()
            if "ceo" in gate_name:
                ceo_gate = "PASS" if row.get("passed") else f"REJECTED ({row.get('detail')})"
                break
        if ceo_gate is _NOT_AVAILABLE:
            if any(str(r).startswith("CEO_GATE") for r in reasons):
                ceo_gate = "REJECTED"
            elif final_action != "NO TRADE" and decision.get("risk_guard_result") not in (
                None,
                "NOT_EVALUATED",
            ):
                ceo_gate = "PASS"
            elif final_action == "NO TRADE" and any("CEO" in str(r).upper() for r in reasons):
                ceo_gate = "REJECTED"

        risk_result = decision.get("risk_guard_result")
        risk_reason = decision.get("risk_guard_reason") or _NOT_AVAILABLE
        if risk_result in (None, "", "NOT_EVALUATED"):
            risk_guard_label = "NOT EVALUATED"
        else:
            risk_guard_label = str(risk_result)

        score = signal.get("score") if isinstance(signal, Mapping) else None
        if score is None and isinstance(signal, Mapping) and isinstance(signal.get("diagnostics"), Mapping):
            score = signal["diagnostics"].get("score")
        if score is None:
            evidence = decision.get("calculated_evidence") or {}
            if isinstance(evidence, Mapping):
                score = evidence.get("score")
        if score is None:
            score = _NOT_AVAILABLE

        option_type = (
            (candidate.get("option_type") if isinstance(candidate, Mapping) else None)
            or (signal.get("option_type") if isinstance(signal, Mapping) else None)
            or _NOT_AVAILABLE
        )
        strike = candidate.get("strike") if isinstance(candidate, Mapping) else None
        if strike is None and isinstance(signal, Mapping):
            strike = signal.get("strike")
        if strike is None:
            strike = _NOT_AVAILABLE
        expiry = (
            (candidate.get("expiry") if isinstance(candidate, Mapping) else None)
            or (signal.get("expiry") if isinstance(signal, Mapping) else None)
            or _NOT_AVAILABLE
        )
        dte = signal.get("dte_days") if isinstance(signal, Mapping) else None
        if dte is None and isinstance(signal, Mapping) and isinstance(signal.get("diagnostics"), Mapping):
            dte = signal["diagnostics"].get("dte_days")
        if dte is None and expiry not in (_NOT_AVAILABLE, None) and self._market_snapshot:
            try:
                exp = date.fromisoformat(str(expiry)[:10])
                sess = self._market_snapshot.get("session_date")
                sess_d = date.fromisoformat(str(sess)[:10]) if sess else session_day(
                    SystemClock().now().astimezone(IST)
                )
                dte = (exp - sess_d).days
            except (TypeError, ValueError):
                dte = _NOT_AVAILABLE
        if dte is None:
            dte = _NOT_AVAILABLE

        instrument = _NOT_AVAILABLE
        if isinstance(candidate, Mapping) and candidate.get("instrument"):
            instrument = candidate["instrument"]
        elif decision.get("candidate_instrument"):
            instrument = decision["candidate_instrument"]
        elif isinstance(signal, Mapping) and signal.get("instrument"):
            instrument = signal["instrument"]

        reason_text = "; ".join(str(r) for r in reasons) if reasons else (
            risk_reason if risk_reason != _NOT_AVAILABLE else _NOT_AVAILABLE
        )
        if final_action == "NO TRADE" and (not reasons) and stale_reason:
            reason_text = stale_reason

        provenance = _NOT_AVAILABLE
        if self._market_snapshot:
            provenance = self._market_snapshot.get("market_data_source") or provenance
        if provenance == _NOT_AVAILABLE and self._replay_record:
            provenance = self._replay_record.get("market_data_provider") or provenance

        cycle_id = decision.get("analysis_cycle_id")
        if cycle_id is None and self.last_cycle is not None:
            lc = (
                self.last_cycle.to_dict()
                if hasattr(self.last_cycle, "to_dict")
                else self.last_cycle
            )
            if isinstance(lc, Mapping):
                cycle_id = lc.get("cycle_id")
        if cycle_id is None:
            cycle_id = _NOT_AVAILABLE

        row = {
            "agent": "DecisionEngine",
            "cycle_id": cycle_id,
            "snapshot_id": decision.get("snapshot_id") or _NOT_AVAILABLE,
            "candidate": instrument,
            "option_type": option_type,
            "ce_pe": option_type,
            "strike": strike,
            "expiry": expiry,
            "dte": dte,
            "score": score,
            "ceo_gate": ceo_gate,
            "risk_guard": risk_guard_label,
            "final_action": final_action,
            "decision": final_action,
            "signal": option_type if final_action != "NO TRADE" else final_action,
            "confidence": (
                candidate.get("confidence")
                if isinstance(candidate, Mapping) and candidate.get("confidence") is not None
                else _NOT_AVAILABLE
            ),
            "timestamp": decision.get("decision_timestamp") or decision.get("as_of") or _NOT_AVAILABLE,
            "reason": reason_text,
            "risk_verdict": risk_result,
            "risk_reason": risk_reason,
            "provenance": provenance,
            "symbol": (
                (candidate.get("underlying") if isinstance(candidate, Mapping) else None)
                or decision.get("candidate_instrument")
            ),
            "data_quality_status": decision.get("data_quality_status") or _NOT_AVAILABLE,
        }
        return redact_secrets(
            {
                "available": True,
                "message": None if final_action != "NO TRADE" else reason_text,
                **base_meta,
                "decisions": [row],
                "last_cycle": None
                if self.last_cycle is None
                else (
                    self.last_cycle.to_dict()
                    if hasattr(self.last_cycle, "to_dict")
                    else dict(self.last_cycle)
                ),
                "replay": {
                    "decision_id": (self._replay_record or {}).get("decision_id"),
                    "schema": (self._replay_record or {}).get("schema"),
                }
                if self._replay_record
                else None,
            }
        )

    def option_chain_view(self) -> dict[str, Any]:
        # Prefer live Kite quotes when Zerodha session is verified CONNECTED.
        zerodha = self._zerodha_public(probe=True)
        connected = str(zerodha.get("status") or "").upper() == "CONNECTED"
        if connected:
            nifty = self._probe_nifty_ltp()
            spot = nifty.get("price")
            if nifty.get("ok") and isinstance(spot, (int, float)):
                live = self._fetch_live_option_chain(spot=float(spot))
                if live.get("ok"):
                    return redact_secrets(live)
                # Fall through to snapshot only as non-LIVE artifact display.
                live_reason = live.get("reason") or live.get("message") or _NOT_AVAILABLE
            else:
                live_reason = nifty.get("reason") or "NIFTY quote unavailable"
        else:
            live_reason = "Zerodha session not CONNECTED"

        snap = self._market_snapshot
        if not isinstance(snap, Mapping):
            return {
                "available": False,
                "message": _NOT_AVAILABLE,
                "label": f"Live option chain unavailable ({live_reason})",
                "data_label": _NOT_AVAILABLE,
                "provenance": _NOT_AVAILABLE,
                "freshness": _NOT_AVAILABLE,
                "rows": [],
                "selected": None,
                "ce": _NOT_AVAILABLE,
                "strike": _NOT_AVAILABLE,
                "pe": _NOT_AVAILABLE,
                "reason": live_reason,
            }

        contracts = snap.get("option_contracts") or []
        session_raw = snap.get("session_date")
        try:
            sess_d = date.fromisoformat(str(session_raw)[:10]) if session_raw else session_day(
                SystemClock().now().astimezone(IST)
            )
        except ValueError:
            sess_d = session_day(SystemClock().now().astimezone(IST))

        score_by_id: dict[str, Any] = {}
        rejection: Any = _NOT_AVAILABLE
        decision = self._decision_payload() or {}
        signal = decision.get("campaign_signal") or {}
        if isinstance(signal, Mapping):
            if signal.get("score") is not None and signal.get("instrument"):
                score_by_id[str(signal["instrument"])] = signal["score"]
            if signal.get("provider_contract_id") and signal.get("score") is not None:
                score_by_id[str(signal["provider_contract_id"])] = signal["score"]
        reasons = list(decision.get("reason_codes") or [])
        for code in reasons:
            text = str(code).upper()
            if "CHAIN" in text or "FILTER" in text or "ELIGIB" in text or "MISSING_OPTION" in text:
                rejection = code
                break

        buckets: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
        for raw in contracts:
            if not isinstance(raw, Mapping):
                continue
            key = (raw.get("underlying"), raw.get("expiry"), raw.get("strike"))
            bucket = buckets.setdefault(
                key,
                {
                    "underlying": raw.get("underlying"),
                    "expiry": raw.get("expiry"),
                    "strike": raw.get("strike"),
                    "dte": _NOT_AVAILABLE,
                    "ce_ltp": _NOT_AVAILABLE,
                    "ce_bid": _NOT_AVAILABLE,
                    "ce_ask": _NOT_AVAILABLE,
                    "pe_ltp": _NOT_AVAILABLE,
                    "pe_bid": _NOT_AVAILABLE,
                    "pe_ask": _NOT_AVAILABLE,
                    "score": _NOT_AVAILABLE,
                    "eligibility": _NOT_AVAILABLE,
                    "rejection_reason": _NOT_AVAILABLE,
                },
            )
            try:
                exp = date.fromisoformat(str(raw.get("expiry"))[:10])
                bucket["dte"] = (exp - sess_d).days
            except (TypeError, ValueError):
                pass
            side = str(raw.get("option_type") or "").upper()
            prefix = "ce" if side == "CE" else "pe" if side == "PE" else None
            if prefix:
                for field in ("ltp", "bid", "ask"):
                    val = raw.get(field)
                    bucket[f"{prefix}_{field}"] = val if val is not None else _NOT_AVAILABLE
            cid = str(raw.get("provider_contract_id") or "")
            if cid and cid in score_by_id:
                bucket["score"] = score_by_id[cid]
            if raw.get("quality") and str(raw.get("quality")).upper() in {
                "STALE",
                "INSUFFICIENT",
                "REJECTED",
            }:
                bucket["eligibility"] = "REJECTED"
                bucket["rejection_reason"] = f"quote_quality:{raw.get('quality')}"
            elif rejection != _NOT_AVAILABLE:
                bucket["eligibility"] = "SEE_DECISION"
                bucket["rejection_reason"] = rejection
            else:
                bucket["eligibility"] = "PRESENT"

        rows = sorted(
            buckets.values(),
            key=lambda r: (str(r.get("expiry")), float(r.get("strike") or 0)),
        )
        selected = None
        cand = decision.get("trade_candidate") or {}
        if cand:
            selected = {
                "strike": cand.get("strike", _NOT_AVAILABLE),
                "expiry": cand.get("expiry", _NOT_AVAILABLE),
                "option_type": cand.get("option_type", _NOT_AVAILABLE),
                "instrument": cand.get("instrument", _NOT_AVAILABLE),
            }
        provenance = snap.get("market_data_source") or _NOT_AVAILABLE
        # Never relabel snapshot FIXTURE/MIXED as LIVE.
        return redact_secrets(
            {
                "available": bool(rows),
                "message": None if rows else "Option chain empty in loaded snapshot",
                "label": (
                    f"{len(rows)} strike(s) from snapshot ({provenance}); "
                    f"live path: {live_reason}"
                ),
                "data_label": provenance,
                "provenance": provenance,
                "freshness": snap.get("data_quality") or _NOT_AVAILABLE,
                "snapshot_id": snap.get("snapshot_id"),
                "rows": rows,
                "selected": selected,
                "ce": selected.get("option_type")
                if selected and selected.get("option_type") == "CE"
                else _NOT_AVAILABLE,
                "strike": (selected or {}).get("strike", _NOT_AVAILABLE),
                "pe": selected.get("option_type")
                if selected and selected.get("option_type") == "PE"
                else _NOT_AVAILABLE,
                "reason": live_reason,
            }
        )

    def settings_view(self, *, probe_zerodha: bool = True) -> dict[str, Any]:
        # Default verified status (TTL) so Settings matches market/system.
        zerodha = self._zerodha_public(probe=True if probe_zerodha else False)
        return {
            "read_only": True,
            "can_enable_live_trading": False,
            "can_place_orders": False,
            "message": (
                "Trading settings remain view-only. "
                "Zerodha Connect authorises market-data only (no orders)."
            ),
            "zerodha_market_data": zerodha,
            "config_summary": self.config_view(),
        }

    def signals_view(self) -> dict[str, Any]:
        decision = self._decision_payload()
        if decision is None:
            return {
                "available": False,
                "message": _NOT_AVAILABLE,
                "signals": [],
                "note": "No campaign signal artifact loaded.",
            }
        signal = decision.get("campaign_signal")
        if not signal:
            return {
                "available": False,
                "message": _NOT_AVAILABLE,
                "signals": [],
                "note": "Decision present but campaign_signal absent.",
            }
        return redact_secrets(
            {
                "available": True,
                "message": None,
                "signals": [dict(signal)],
                "note": "Campaign signal from last decision/replay.",
            }
        )

    def research_view(self) -> dict[str, Any]:
        return {
            "available": False,
            "message": _NOT_AVAILABLE,
            "research_label": "HISTORICAL RESEARCH / NOT LIVE",
            "note": "Research artifacts are not streamed into the dashboard read model.",
        }

    def dashboard(self) -> dict[str, Any]:
        base = _snapshot(self.config, self.book, self.last_cycle)
        market = self.market_status()
        risk = self.risk_view()
        body = {
            "safety": self.safety(),
            "system": {
                "trading_mode": self.config.execution.mode,
                "live_trading_status": "DISABLED",
                "live_trading_compiled": bool(LIVE_TRADING_COMPILED),
                "broker_order_path": "DISABLED",
                "risk_guard_status": risk.get("status_label") or "ACTIVE",
                "market_data_status": market.get("market_data"),
                "zerodha_status": market.get("zerodha_status"),
                "provenance": market.get("provenance"),
                "freshness": market.get("freshness"),
            },
            "capital": {
                "paper_capital": self.config.paper.starting_cash,
                "starting_capital": self.config.paper.starting_cash,
                "available_capital": self.book.cash,
                "today_pnl": risk["today_pnl"],
                "daily_loss_limit": self.config.risk.max_daily_loss,
                "max_daily_loss": self.config.risk.max_daily_loss,
                "max_per_trade_risk": self.config.risk.max_per_trade_risk,
                "max_open_positions": self.config.risk.max_open_positions,
                "remaining_daily_risk": risk["remaining_daily_risk"],
                "currency": self.config.market.currency,
                "capital_profile": self.config.paper.capital_profile,
                "data_label": market.get("provenance") or market.get("data_label"),
            },
            "market": market,
            "positions": self.positions_view(),
            "risk": risk,
            "agents": self.agents_view(),
            "option_chain": self.option_chain_view(),
            "signals": self.signals_view(),
            "research": self.research_view(),
            "snapshot": base,
            "checkpoint": self._checkpoint_meta,
        }
        return redact_secrets(body)


def build_service_from_environ(
    environ: Mapping[str, str] | None = None,
    *,
    config_path: Path | str | None = None,
    checkpoint_path: Path | str | None = None,
) -> DashboardService:
    """Factory used by the ASGI app and tests."""
    from grow.config import apply_dotenv

    if environ is None:
        apply_dotenv()
        env = dict(os.environ)
    else:
        env = dict(environ)
    # Dashboard never reads broker credentials; scrub before the paper boot lock.
    env = scrub_broker_credentials_for_paper(env)
    config = load_config(config_path, environ=env, scrub_broker_credentials=False)
    return DashboardService(config, checkpoint_path=checkpoint_path)


def dumps_safe(payload: Mapping[str, Any]) -> str:
    return json.dumps(redact_secrets(payload), indent=2, default=str, sort_keys=True)
