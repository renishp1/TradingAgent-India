"""Configuration loader.

YAML is the documented source of truth. The parser is a small indent-based
subset so milestone 1 stays stdlib-only. Environment variables overlay the
file and then the safety lock inspects the result.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grow.errors import GrowConfigError, GrowLiveTradingDisabled
from grow.execution.lock import inspect_environment, normalize_execution_mode

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "grow.default.yaml"


def _parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if value in {"true", "True", "yes"}:
        return True
    if value in {"false", "False", "no"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse a restricted YAML subset: comments, nested maps, and string lists."""
    lines: list[tuple[int, str]] = []
    for lineno, original in enumerate(text.splitlines(), start=1):
        stripped = original.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        indent = len(original) - len(original.lstrip(" "))
        if indent % 2 != 0:
            raise GrowConfigError(f"YAML indent must be multiples of 2 (line {lineno})")
        lines.append((indent, stripped.strip()))

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        mapping: dict[str, Any] = {}
        sequence: list[Any] | None = None
        while index < len(lines):
            current_indent, content = lines[index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise GrowConfigError(f"Unexpected indent at {content!r}")
            if content.startswith("- "):
                if mapping:
                    raise GrowConfigError("Cannot mix map and list at the same level")
                if sequence is None:
                    sequence = []
                sequence.append(_parse_scalar(content[2:]))
                index += 1
                continue
            if sequence is not None:
                raise GrowConfigError("Cannot mix list and map at the same level")
            if ":" not in content:
                raise GrowConfigError(f"Expected key: value, got {content!r}")
            key, rest = content.split(":", 1)
            key = key.strip()
            rest = rest.strip()
            index += 1
            if rest == "":
                if index < len(lines) and lines[index][0] > current_indent:
                    child, index = parse_block(index, current_indent + 2)
                    mapping[key] = child
                else:
                    mapping[key] = {}
            else:
                mapping[key] = _parse_scalar(rest)
        return (sequence if sequence is not None else mapping), index

    root, _ = parse_block(0, 0)
    if not isinstance(root, dict):
        raise GrowConfigError("Top-level YAML must be a mapping")
    return root


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise GrowConfigError(f"{key} must be a boolean")


def _as_float(value: Any, key: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise GrowConfigError(f"{key} must be a number") from exc


def _as_int(value: Any, key: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise GrowConfigError(f"{key} must be an integer") from exc


@dataclass(frozen=True)
class ExecutionConfig:
    mode: str
    live_trading_enabled: bool


@dataclass(frozen=True)
class MarketConfig:
    exchange: str
    currency: str
    session_open: str
    session_close: str
    square_off: str
    universe: tuple[str, ...]
    product: str


@dataclass(frozen=True)
class RiskConfig:
    max_position_notional: float
    max_gross_notional: float
    max_daily_loss: float
    require_stop_loss: bool
    max_symbol_concentration: float
    ruleset: str
    allow_short: bool
    concentration_basis: str


@dataclass(frozen=True)
class PaperConfig:
    starting_cash: float
    venue_id: str


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    deep_model: str
    fast_model: str
    max_output_tokens: int


@dataclass(frozen=True)
class TradingAgentsConfig:
    enabled: bool
    max_debate_rounds: int
    max_risk_discuss_rounds: int


@dataclass(frozen=True)
class DataConfig:
    provider: str
    allow_live_feed: bool
    allow_options_chain: bool
    timeframes: tuple[str, ...]
    stale_after_seconds: int
    history_sessions: int


@dataclass(frozen=True)
class GrowConfig:
    version: str
    timezone: str
    execution: ExecutionConfig
    market: MarketConfig
    risk: RiskConfig
    paper: PaperConfig
    model: ModelConfig
    tradingagents: TradingAgentsConfig
    data: DataConfig
    source_path: str

    def assert_safe(self) -> None:
        normalize_execution_mode(self.execution.mode)
        if self.execution.live_trading_enabled:
            raise GrowLiveTradingDisabled("config.execution.live_trading_enabled must be false")
        if self.paper.venue_id.upper() not in {"GROW_PAPER", "PAPER"}:
            raise GrowLiveTradingDisabled(f"Unknown paper venue {self.paper.venue_id!r}")
        if self.market.product != "CASH":
            raise GrowConfigError("Milestone 1 product must be CASH.")
        if self.risk.allow_short:
            raise GrowConfigError("Milestone 1 cash book forbids shorts. allow_short must be false.")
        if self.risk.concentration_basis != "cost_notional":
            raise GrowConfigError("Milestone 1 concentration_basis must be cost_notional until MTM exists.")
        if self.data.provider != "fixture":
            raise GrowConfigError(
                "Milestone 2A data.provider must be 'fixture' until a licensed feed is reviewed."
            )
        if self.data.allow_live_feed:
            raise GrowConfigError("Live market feeds are not attached. allow_live_feed must be false.")
        if self.data.allow_options_chain:
            raise GrowConfigError("Options chains are not part of 2A cash data.")
        allowed_tf = {"D1", "M15", "M5"}
        if not self.data.timeframes or any(tf not in allowed_tf for tf in self.data.timeframes):
            raise GrowConfigError("2A timeframes must be a non-empty subset of D1, M15, M5.")


def _overlay_env(raw: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    grow = raw.setdefault("grow", {})
    execution = grow.setdefault("execution", {})
    market = grow.setdefault("market", {})
    paper = grow.setdefault("paper", {})
    model = grow.setdefault("model", {})
    ta = grow.setdefault("tradingagents", {})
    data = grow.setdefault("data", {})

    if "GROW_EXECUTION_MODE" in environ and environ["GROW_EXECUTION_MODE"].strip():
        execution["mode"] = environ["GROW_EXECUTION_MODE"]
    if "GROW_LIVE_TRADING" in environ and environ["GROW_LIVE_TRADING"].strip():
        execution["live_trading_enabled"] = _parse_scalar(environ["GROW_LIVE_TRADING"])
    if "LIVE_TRADING_ENABLED" in environ and environ["LIVE_TRADING_ENABLED"].strip():
        execution["live_trading_enabled"] = _parse_scalar(environ["LIVE_TRADING_ENABLED"])
    if environ.get("GROW_TIMEZONE"):
        grow["timezone"] = environ["GROW_TIMEZONE"]
    if environ.get("GROW_EXCHANGE"):
        market["exchange"] = environ["GROW_EXCHANGE"]
    if environ.get("GROW_STARTING_CASH"):
        paper["starting_cash"] = _parse_scalar(environ["GROW_STARTING_CASH"])
    if environ.get("GROW_MODEL_PROVIDER"):
        model["provider"] = environ["GROW_MODEL_PROVIDER"]
    if environ.get("GROW_TRADINGAGENTS_ENABLED"):
        ta["enabled"] = _parse_scalar(environ["GROW_TRADINGAGENTS_ENABLED"])
    if environ.get("GROW_MAX_DEBATE_ROUNDS"):
        ta["max_debate_rounds"] = _parse_scalar(environ["GROW_MAX_DEBATE_ROUNDS"])
    if environ.get("GROW_DATA_PROVIDER"):
        data["provider"] = environ["GROW_DATA_PROVIDER"]
    if environ.get("GROW_ALLOW_LIVE_FEED"):
        data["allow_live_feed"] = _parse_scalar(environ["GROW_ALLOW_LIVE_FEED"])
    return raw


def _build(raw: dict[str, Any], source_path: str) -> GrowConfig:
    if "grow" not in raw or not isinstance(raw["grow"], dict):
        raise GrowConfigError("Config must have a top-level 'grow' mapping")
    g = raw["grow"]
    execution = g.get("execution") or {}
    market = g.get("market") or {}
    risk = g.get("risk") or {}
    paper = g.get("paper") or {}
    model = g.get("model") or {}
    ta = g.get("tradingagents") or {}
    data = g.get("data") or {}
    universe = market.get("universe") or []
    if not isinstance(universe, list) or not universe:
        raise GrowConfigError("grow.market.universe must be a non-empty list")

    timeframes = data.get("timeframes") or ["D1", "M15", "M5"]
    if not isinstance(timeframes, list):
        raise GrowConfigError("grow.data.timeframes must be a list")

    config = GrowConfig(
        version=str(g.get("version", "0.1.0")),
        timezone=str(g.get("timezone", "Asia/Kolkata")),
        execution=ExecutionConfig(
            mode=normalize_execution_mode(str(execution.get("mode", "paper"))),
            live_trading_enabled=_as_bool(execution.get("live_trading_enabled", False), "live_trading_enabled"),
        ),
        market=MarketConfig(
            exchange=str(market.get("exchange", "NSE")).upper(),
            currency=str(market.get("currency", "INR")).upper(),
            session_open=str(market.get("session_open", "09:15")),
            session_close=str(market.get("session_close", "15:30")),
            square_off=str(market.get("square_off", "15:15")),
            universe=tuple(str(s).strip().upper() for s in universe),
            product=str(market.get("product", "CASH")).upper(),
        ),
        risk=RiskConfig(
            max_position_notional=_as_float(risk.get("max_position_notional", 100000), "max_position_notional"),
            max_gross_notional=_as_float(risk.get("max_gross_notional", 300000), "max_gross_notional"),
            max_daily_loss=_as_float(risk.get("max_daily_loss", 15000), "max_daily_loss"),
            require_stop_loss=_as_bool(risk.get("require_stop_loss", True), "require_stop_loss"),
            max_symbol_concentration=_as_float(
                risk.get("max_symbol_concentration", 0.35), "max_symbol_concentration"
            ),
            ruleset=str(risk.get("ruleset", "grow.risk.v1")),
            allow_short=_as_bool(risk.get("allow_short", False), "allow_short"),
            concentration_basis=str(risk.get("concentration_basis", "cost_notional")),
        ),
        paper=PaperConfig(
            starting_cash=_as_float(paper.get("starting_cash", 1_000_000), "starting_cash"),
            venue_id=str(paper.get("venue_id", "GROW_PAPER")),
        ),
        model=ModelConfig(
            provider=str(model.get("provider", "mock")).lower(),
            deep_model=str(model.get("deep_model", "mock-deep")),
            fast_model=str(model.get("fast_model", "mock-fast")),
            max_output_tokens=_as_int(model.get("max_output_tokens", 1024), "max_output_tokens"),
        ),
        tradingagents=TradingAgentsConfig(
            enabled=_as_bool(ta.get("enabled", True), "tradingagents.enabled"),
            max_debate_rounds=_as_int(ta.get("max_debate_rounds", 1), "max_debate_rounds"),
            max_risk_discuss_rounds=_as_int(ta.get("max_risk_discuss_rounds", 1), "max_risk_discuss_rounds"),
        ),
        data=DataConfig(
            provider=str(data.get("provider", "fixture")).lower(),
            allow_live_feed=_as_bool(data.get("allow_live_feed", False), "data.allow_live_feed"),
            allow_options_chain=_as_bool(data.get("allow_options_chain", False), "data.allow_options_chain"),
            timeframes=tuple(str(tf).strip().upper() for tf in timeframes),
            stale_after_seconds=_as_int(data.get("stale_after_seconds", 900), "data.stale_after_seconds"),
            history_sessions=_as_int(data.get("history_sessions", 20), "data.history_sessions"),
        ),
        source_path=source_path,
    )
    config.assert_safe()
    return config


def load_config(
    path: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> GrowConfig:
    env = dict(os.environ if environ is None else environ)
    inspect_environment(env.items())
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    text = config_path.read_text(encoding="utf-8")
    raw = parse_simple_yaml(text)
    raw = _overlay_env(raw, env)
    return _build(raw, str(config_path))
