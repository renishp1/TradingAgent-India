"""Read-only dashboard views.

Consumes existing config, PaperBook snapshots, and safety lock state.
Does not mint RiskStamps, place fills, or talk to brokers.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from grow.config import GrowConfig, load_config
from grow.execution.lock import LIVE_TRADING_COMPILED, scrub_broker_credentials_for_paper
from grow.market.session import SessionCalendar
from grow.paper.checkpoint import load_paper_checkpoint
from grow.paper.ledger import PaperBook, Position
from grow.types import Symbol


def _lock_status() -> dict[str, Any]:
    # Lazy import avoids circular import with grow.dashboard.__init__.
    from grow.dashboard import lock_status

    return lock_status()


def _snapshot(config: GrowConfig, book: PaperBook, last_cycle: Any | None = None) -> dict[str, Any]:
    from grow.dashboard import snapshot

    return snapshot(config, book, last_cycle)

_NOT_AVAILABLE = "Not available"

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
    """In-process read model for the paper dashboard (Phase 1)."""

    def __init__(
        self,
        config: GrowConfig | None = None,
        *,
        book: PaperBook | None = None,
        last_cycle: Any | None = None,
        checkpoint_path: Path | str | None = None,
        option_positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.config = config or load_config()
        self.config.assert_safe()
        self.checkpoint_path = self._resolve_checkpoint_path(checkpoint_path)
        self.last_cycle = last_cycle
        self._engine_positions: list[dict[str, Any]] = list(option_positions or [])
        self._checkpoint_meta: dict[str, Any] | None = None
        self._last_risk_daily_pnl: float | None = None
        self.book = book if book is not None else _empty_book(self.config)
        if book is None:
            self._try_load_checkpoint()

    @staticmethod
    def _resolve_checkpoint_path(explicit: Path | str | None) -> Path | None:
        if explicit is not None:
            return Path(explicit)
        env = (os.environ.get("GROW_DASHBOARD_CHECKPOINT") or "").strip()
        if env:
            return Path(env)
        default = Path.cwd() / "results" / "paper_checkpoint.json"
        return default if default.is_file() else None

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
            "ui_phase": 1,
            "ui_mode": "read_only",
            "canonical_paper_path": "campaign",
            "note": (
                "Phase 1 dashboard is read-only. No buy/sell controls. "
                "Canonical paper path is CampaignRunner "
                "(Analysis → CampaignOptions → Decision → CEO gate → RiskGuard → paper)."
            ),
        }

    def market_status(self) -> dict[str, Any]:
        calendar = SessionCalendar(self.config.market)
        state = calendar.state()
        live_connected = bool(self.config.live_data.enabled and self.config.data.allow_live_feed)
        # Phase 1 dashboard never attaches a live feed itself.
        return {
            "exchange": self.config.market.exchange,
            "session_state": state.value,
            "session_open": self.config.market.session_open,
            "session_close": self.config.market.session_close,
            "square_off": self.config.market.square_off,
            "timezone": self.config.timezone,
            "market_data": "NOT CONNECTED" if not live_connected else "CONFIGURED (not connected by UI)",
            "market_data_connected": False,
            "data_provider": self.config.data.provider,
            "data_label": "FIXTURE / PAPER DATA" if self.config.data.provider == "fixture" else "PAPER DATA",
            "indices": {
                "NIFTY": {"price": _NOT_AVAILABLE, "label": "Not available"},
                "BANKNIFTY": {"price": _NOT_AVAILABLE, "label": "Not available"},
            },
            "note": "Dashboard Phase 1 does not connect to live market data.",
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
                "current_exposure": book.gross_notional,
                "open_positions": open_count,
                "today_pnl": today_pnl,
                "realized_pnl_at_cost": realized,
                "remaining_daily_risk": remaining,
                "require_stop_loss": risk.require_stop_loss,
                "allow_short": risk.allow_short,
                "read_only": True,
                "note": (
                    "Limits come from GrowConfig.risk / paper. "
                    "True daily P&L may be Not available until valuation exists."
                ),
            }
        )

    def agents_view(self) -> dict[str, Any]:
        cycle = self.last_cycle
        if cycle is None:
            return {
                "available": False,
                "message": "AI decision data not available",
                "model_provider": self.config.model.provider,
                "tradingagents_enabled": self.config.tradingagents.enabled,
                "decisions": [],
            }

        payload = cycle.to_dict() if hasattr(cycle, "to_dict") else dict(cycle)
        proposal = payload.get("proposal") or {}
        verdict = payload.get("verdict") or {}
        return redact_secrets(
            {
                "available": True,
                "message": None,
                "model_provider": self.config.model.provider,
                "tradingagents_enabled": self.config.tradingagents.enabled,
                "decisions": [
                    {
                        "agent": "CEO",
                        "decision": proposal.get("intent") or _NOT_AVAILABLE,
                        "signal": proposal.get("side") or _NOT_AVAILABLE,
                        "confidence": proposal.get("confidence")
                        if proposal.get("confidence") is not None
                        else _NOT_AVAILABLE,
                        "timestamp": proposal.get("created_at") or _NOT_AVAILABLE,
                        "reason": proposal.get("thesis") or _NOT_AVAILABLE,
                        "risk_verdict": verdict.get("approved"),
                        "risk_reason": verdict.get("reason") or _NOT_AVAILABLE,
                        "symbol": payload.get("symbol") or proposal.get("symbol"),
                    }
                ],
                "last_cycle": payload,
            }
        )

    def option_chain_view(self) -> dict[str, Any]:
        return {
            "available": False,
            "message": "Not available",
            "label": "Option chain not connected in dashboard Phase 1",
            "data_label": "FIXTURE / PAPER DATA",
            "selected": None,
            "ce": _NOT_AVAILABLE,
            "strike": _NOT_AVAILABLE,
            "pe": _NOT_AVAILABLE,
        }

    def signals_view(self) -> dict[str, Any]:
        return {
            "available": False,
            "message": "Not available",
            "signals": [],
            "note": "SignalEngine output is not attached to the Phase 1 dashboard.",
        }

    def research_view(self) -> dict[str, Any]:
        return {
            "available": False,
            "message": "Not available",
            "research_label": "HISTORICAL RESEARCH / NOT LIVE",
            "note": "Research artifacts are not streamed into the Phase 1 dashboard.",
        }

    def settings_view(self, *, probe_zerodha: bool = False) -> dict[str, Any]:
        from grow.dashboard.zerodha_auth import auth_status

        zerodha = auth_status(probe=probe_zerodha).to_public_dict()
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

    def dashboard(self) -> dict[str, Any]:
        base = _snapshot(self.config, self.book, self.last_cycle)
        body = {
            "safety": self.safety(),
            "system": {
                "trading_mode": self.config.execution.mode,
                "live_trading_status": "DISABLED",
                "live_trading_compiled": bool(LIVE_TRADING_COMPILED),
                "broker_order_path": "DISABLED",
                "risk_guard_status": "ACTIVE",
                "market_data_status": "NOT CONNECTED",
            },
            "capital": {
                "paper_capital": self.config.paper.starting_cash,
                "available_capital": self.book.cash,
                "today_pnl": self.risk_view()["today_pnl"],
                "daily_loss_limit": self.config.risk.max_daily_loss,
                "remaining_daily_risk": self.risk_view()["remaining_daily_risk"],
                "currency": self.config.market.currency,
                "data_label": "FIXTURE / PAPER DATA",
            },
            "market": self.market_status(),
            "positions": self.positions_view(),
            "risk": self.risk_view(),
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
