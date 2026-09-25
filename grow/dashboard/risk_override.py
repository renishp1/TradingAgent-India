"""Paper risk overrides for the dashboard UI.

Persists operator-chosen paper capital / risk limits without enabling live trading
or broker orders. Applied after the campaign capital profile so UI values win.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from grow.config import GrowConfig
from grow.errors import GrowConfigError

OVERRIDE_PROFILE = "UI_PAPER_OVERRIDE"
ALLOWED_KEYS = frozenset(
    {
        "starting_cash",
        "max_daily_loss",
        "max_per_trade_risk",
        "max_open_positions",
    }
)
_BANNED_KEYS = frozenset(
    {
        "live_trading",
        "live_trading_enabled",
        "place_order",
        "order",
        "broker",
        "execution_mode",
    }
)
_MAX_CASH = 10_000_000.0
_MAX_RISK = 1_000_000.0
_MAX_POSITIONS = 50


def resolve_override_path(*, cwd: Path | None = None) -> Path:
    import os

    explicit = (os.environ.get("GROW_DASHBOARD_RISK_OVERRIDE") or "").strip()
    if explicit:
        return Path(explicit)
    root = cwd if cwd is not None else Path.cwd()
    return root / "results" / "dashboard_risk_override.json"


def load_override(path: Path | None = None) -> dict[str, float | int] | None:
    target = path if path is not None else resolve_override_path()
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        cleaned = _normalize_payload(raw, partial=True, allow_meta=True)
    except GrowConfigError:
        return None
    return cleaned or None


def save_override(payload: Mapping[str, Any], path: Path | None = None) -> Path:
    cleaned = _normalize_payload(payload, partial=False, allow_meta=False)
    target = path if path is not None else resolve_override_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": 1,
        "capital_profile": OVERRIDE_PROFILE,
        "paper_only": True,
        "live_trading": False,
        "broker_order_path": False,
        **cleaned,
    }
    target.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def clear_override(path: Path | None = None) -> bool:
    target = path if path is not None else resolve_override_path()
    if not target.is_file():
        return False
    target.unlink()
    return True


def apply_override(config: GrowConfig, override: Mapping[str, Any] | None) -> GrowConfig:
    if not override:
        return config
    cleaned = _normalize_payload(override, partial=True, allow_meta=True)
    if not cleaned:
        return config
    # Keep capital_profile None so assert_safe accepts custom UI limits.
    paper_kwargs: dict[str, Any] = {"capital_profile": None}
    risk_kwargs: dict[str, Any] = {}
    if "starting_cash" in cleaned:
        paper_kwargs["starting_cash"] = float(cleaned["starting_cash"])
    if "max_daily_loss" in cleaned:
        risk_kwargs["max_daily_loss"] = float(cleaned["max_daily_loss"])
    if "max_per_trade_risk" in cleaned:
        risk_kwargs["max_per_trade_risk"] = float(cleaned["max_per_trade_risk"])
    if "max_open_positions" in cleaned:
        risk_kwargs["max_open_positions"] = int(cleaned["max_open_positions"])
    return replace(
        config,
        paper=replace(config.paper, **paper_kwargs),
        risk=replace(config.risk, **risk_kwargs) if risk_kwargs else config.risk,
    )


def _normalize_payload(
    raw: Mapping[str, Any],
    *,
    partial: bool,
    allow_meta: bool = False,
) -> dict[str, float | int]:
    for banned in _BANNED_KEYS:
        if banned not in raw:
            continue
        val = raw[banned]
        # Stored meta may include live_trading: false; only refuse enabling flips.
        if allow_meta and banned in {"live_trading", "live_trading_enabled"} and not val:
            continue
        if allow_meta and banned in {"paper_only"} and val:
            continue
        raise GrowConfigError(f"REFUSED: {banned} cannot be set via paper risk UI")
    meta = {
        "version",
        "capital_profile",
        "paper_only",
        "live_trading",
        "broker_order_path",
        "note",
        "updated_at",
    }
    out: dict[str, float | int] = {}
    if "starting_cash" in raw:
        out["starting_cash"] = _pos_float(raw["starting_cash"], "starting_cash", _MAX_CASH)
    if "max_daily_loss" in raw:
        out["max_daily_loss"] = _pos_float(raw["max_daily_loss"], "max_daily_loss", _MAX_RISK)
    if "max_per_trade_risk" in raw:
        out["max_per_trade_risk"] = _pos_float(
            raw["max_per_trade_risk"], "max_per_trade_risk", _MAX_RISK
        )
    if "max_open_positions" in raw:
        out["max_open_positions"] = _pos_int(
            raw["max_open_positions"], "max_open_positions", _MAX_POSITIONS
        )
    if not partial:
        missing = ALLOWED_KEYS - set(out)
        if missing:
            raise GrowConfigError(f"Missing risk fields: {', '.join(sorted(missing))}")
    unknown = set(raw) - ALLOWED_KEYS - meta
    if unknown:
        raise GrowConfigError(f"Unknown risk fields: {', '.join(sorted(unknown))}")
    return out


def _pos_float(value: Any, name: str, upper: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise GrowConfigError(f"{name} must be a number") from exc
    if num <= 0:
        raise GrowConfigError(f"{name} must be > 0")
    if num > upper:
        raise GrowConfigError(f"{name} must be <= {upper:g}")
    return round(num, 2)


def _pos_int(value: Any, name: str, upper: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError) as exc:
        raise GrowConfigError(f"{name} must be an integer") from exc
    if num < 1:
        raise GrowConfigError(f"{name} must be >= 1")
    if num > upper:
        raise GrowConfigError(f"{name} must be <= {upper}")
    return num


__all__ = [
    "ALLOWED_KEYS",
    "OVERRIDE_PROFILE",
    "apply_override",
    "clear_override",
    "load_override",
    "resolve_override_path",
    "save_override",
]
