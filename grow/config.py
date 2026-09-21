"""Configuration loader.

YAML is the documented source of truth. The parser is a small indent-based
subset so milestone 1 stays stdlib-only. Environment variables overlay the
file and then the safety lock inspects the result.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

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
class StrategySpec:
    name: str
    enabled: bool
    version: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class StrategiesConfig:
    universe: tuple[str, ...]
    primary_timeframe: str
    supported_timeframes: tuple[str, ...]
    specs: tuple[StrategySpec, ...]


@dataclass(frozen=True)
class OptionsConfig:
    enabled: bool
    provider: str
    allow_live_chain: bool
    max_chain_age_minutes: int
    max_quote_age_minutes: int
    allow_same_day: bool
    preferred_expiry_class: str
    max_distance_from_atm: int
    allowed_moneyness: tuple[str, ...]
    min_volume: int
    min_open_interest: int
    max_spread_pct: float
    iv_min: float
    iv_max: float
    scoring_version: str
    safety_reject_stale: bool
    safety_reject_invalid_quotes: bool
    selection_version: str


@dataclass(frozen=True)
class AIConfig:
    enabled: bool
    provider: str
    deterministic_mode: bool
    max_retries: int
    timeout_seconds: int
    confidence_enabled: bool
    min_ceo_confidence: float
    prompt_versions: Mapping[str, str]
    allow_broker: bool
    allow_live_trading: bool
    allow_ai_execution: bool


@dataclass(frozen=True)
class BacktestConfig:
    enabled: bool
    provider: str
    fill_model: str
    slippage_bps: float
    strict: bool
    quantity: int
    lot_size: int
    max_open_positions: int
    starting_cash: float
    cost_model_version: str
    slippage_model_version: str
    train_sessions: int
    validate_sessions: int
    test_sessions: int
    step_sessions: int
    embargo_sessions: int
    calibrate_on_test: bool


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
    strategies: StrategiesConfig
    options: OptionsConfig
    ai: AIConfig
    backtest: BacktestConfig
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
        allowed_idx = {"NIFTY", "BANKNIFTY"}
        if not self.strategies.universe or any(s not in allowed_idx for s in self.strategies.universe):
            raise GrowConfigError("2B strategy universe must be a non-empty subset of NIFTY, BANKNIFTY.")
        if self.strategies.primary_timeframe != "M15":
            raise GrowConfigError("2B primary_timeframe is locked to M15. M5/D1 are context only.")
        if tuple(self.strategies.supported_timeframes) != ("M5", "M15", "D1"):
            raise GrowConfigError("2B supported_timeframes must be M5, M15, D1.")
        if self.data.allow_options_chain or self.options.allow_live_chain:
            raise GrowConfigError("Live option chains are not attached.")
        if self.options.provider != "fixture":
            raise GrowConfigError("2C options.provider must be 'fixture'.")
        if self.options.preferred_expiry_class != "weekly":
            raise GrowConfigError("2C preferred_expiry_class is weekly.")
        if self.options.allow_same_day:
            raise GrowConfigError("2C forbids same-day expiry.")
        if self.options.max_distance_from_atm < 0:
            raise GrowConfigError("max_distance_from_atm must be >= 0")
        if self.ai.provider != "fixture":
            raise GrowConfigError("2D ai.provider must be 'fixture'.")
        if self.ai.allow_broker or self.ai.allow_live_trading or self.ai.allow_ai_execution:
            raise GrowConfigError("2D AI may not enable broker, live trading, or execution.")
        if self.backtest.provider != "fixture":
            raise GrowConfigError("2E backtest.provider must be 'fixture'.")
        if self.backtest.fill_model != "ask_plus_slippage":
            raise GrowConfigError("2E fill_model must be ask_plus_slippage.")
        if self.backtest.max_open_positions != 1:
            raise GrowConfigError("2E v1 allows one open position.")
        if self.backtest.calibrate_on_test:
            raise GrowConfigError("2E must not calibrate on the test window.")
        if self.backtest.quantity < 1:
            raise GrowConfigError("2E quantity (lots) must be >= 1")
        if self.backtest.lot_size < 1:
            raise GrowConfigError("2E lot_size (contract multiplier) must be >= 1")



_DEFAULT_STRATEGY_SPECS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("ema_trend", {"enabled": True, "version": "v1", "fast_period": 20, "slow_period": 50, "atr_period": 14}),
    (
        "momentum",
        {
            "enabled": True,
            "version": "v1",
            "rsi_period": 14,
            "rsi_threshold": 55,
            "roc_period": 10,
            "atr_period": 14,
        },
    ),
    ("breakout", {"enabled": True, "version": "v1", "lookback": 20, "atr_period": 14}),
    (
        "mean_reversion",
        {
            "enabled": True,
            "version": "v1",
            "rsi_period": 14,
            "rsi_oversold": 30,
            "deviation": 0.01,
            "atr_period": 14,
        },
    ),
)


def _strategy_specs(raw: dict[str, Any]) -> tuple[StrategySpec, ...]:
    reserved = {"universe", "primary_timeframe", "supported_timeframes"}
    found = {k: v for k, v in raw.items() if k not in reserved}
    if not found:
        found = {name: body for name, body in _DEFAULT_STRATEGY_SPECS}
    specs: list[StrategySpec] = []
    for name, body in found.items():
        if not isinstance(body, dict):
            raise GrowConfigError(f"grow.strategies.{name} must be a mapping")
        params = {k: v for k, v in body.items() if k not in {"enabled", "version"}}
        specs.append(
            StrategySpec(
                name=str(name),
                enabled=_as_bool(body.get("enabled", True), f"strategies.{name}.enabled"),
                version=str(body.get("version", "v1")),
                params=params,
            )
        )
    return tuple(specs)


def _options_config(raw: dict[str, Any]) -> OptionsConfig:
    if not isinstance(raw, dict):
        raise GrowConfigError("grow.options must be a mapping")
    fresh = raw.get("freshness") or {}
    expiry = raw.get("expiry") or {}
    strikes = raw.get("strikes") or {}
    liq = raw.get("liquidity") or {}
    iv = raw.get("iv") or {}
    scoring = raw.get("scoring") or {}
    safety = raw.get("safety") or {}
    moneyness = strikes.get("allowed_moneyness") or ["ATM", "ITM", "OTM"]
    if not isinstance(moneyness, list):
        raise GrowConfigError("options.strikes.allowed_moneyness must be a list")
    return OptionsConfig(
        enabled=_as_bool(raw.get("enabled", True), "options.enabled"),
        provider=str(raw.get("provider", "fixture")).lower(),
        allow_live_chain=_as_bool(raw.get("allow_live_chain", False), "options.allow_live_chain"),
        max_chain_age_minutes=_as_int(fresh.get("max_chain_age_minutes", 5), "options.freshness.max_chain_age_minutes"),
        max_quote_age_minutes=_as_int(fresh.get("max_quote_age_minutes", 5), "options.freshness.max_quote_age_minutes"),
        allow_same_day=_as_bool(expiry.get("allow_same_day", False), "options.expiry.allow_same_day"),
        preferred_expiry_class=str(expiry.get("preferred_expiry_class", "weekly")).lower(),
        max_distance_from_atm=_as_int(strikes.get("max_distance_from_atm", 2), "options.strikes.max_distance_from_atm"),
        allowed_moneyness=tuple(str(m).strip().upper() for m in moneyness),
        min_volume=_as_int(liq.get("min_volume", 100), "options.liquidity.min_volume"),
        min_open_interest=_as_int(liq.get("min_open_interest", 500), "options.liquidity.min_open_interest"),
        max_spread_pct=_as_float(liq.get("max_spread_pct", 0.08), "options.liquidity.max_spread_pct"),
        iv_min=_as_float(iv.get("min", 0.05), "options.iv.min"),
        iv_max=_as_float(iv.get("max", 2.0), "options.iv.max"),
        scoring_version=str(scoring.get("version", "v1")),
        safety_reject_stale=_as_bool(safety.get("reject_stale_chain", True), "options.safety.reject_stale_chain"),
        safety_reject_invalid_quotes=_as_bool(
            safety.get("reject_invalid_quotes", True), "options.safety.reject_invalid_quotes"
        ),
        selection_version=str(raw.get("selection_version", "options.select.v1")),
    )


def _ai_config(raw: dict[str, Any]) -> AIConfig:
    if not isinstance(raw, dict):
        raise GrowConfigError("grow.ai must be a mapping")
    confidence = raw.get("confidence") or {}
    prompts = raw.get("prompt_versions") or {}
    safety = raw.get("safety") or {}
    if not isinstance(prompts, dict):
        raise GrowConfigError("ai.prompt_versions must be a mapping")
    versions = {
        "bull": str(prompts.get("bull", "v1")),
        "bear": str(prompts.get("bear", "v1")),
        "quant": str(prompts.get("quant", "v1")),
        "risk_context": str(prompts.get("risk_context", "v1")),
        "ceo": str(prompts.get("ceo", "v1")),
    }
    return AIConfig(
        enabled=_as_bool(raw.get("enabled", True), "ai.enabled"),
        provider=str(raw.get("provider", "fixture")).lower(),
        deterministic_mode=_as_bool(raw.get("deterministic_mode", True), "ai.deterministic_mode"),
        max_retries=_as_int(raw.get("max_retries", 0), "ai.max_retries"),
        timeout_seconds=_as_int(raw.get("timeout_seconds", 30), "ai.timeout_seconds"),
        confidence_enabled=_as_bool(confidence.get("enabled", False), "ai.confidence.enabled"),
        min_ceo_confidence=_as_float(confidence.get("min_ceo_confidence", 0.0), "ai.confidence.min_ceo_confidence"),
        prompt_versions=versions,
        allow_broker=_as_bool(safety.get("allow_broker", False), "ai.safety.allow_broker"),
        allow_live_trading=_as_bool(safety.get("allow_live_trading", False), "ai.safety.allow_live_trading"),
        allow_ai_execution=_as_bool(safety.get("allow_ai_execution", False), "ai.safety.allow_ai_execution"),
    )


def _backtest_config(raw: dict[str, Any]) -> BacktestConfig:
    if not isinstance(raw, dict):
        raise GrowConfigError("grow.backtest must be a mapping")
    wf = raw.get("walk_forward") or {}
    return BacktestConfig(
        enabled=_as_bool(raw.get("enabled", True), "backtest.enabled"),
        provider=str(raw.get("provider", "fixture")).lower(),
        fill_model=str(raw.get("fill_model", "ask_plus_slippage")).lower(),
        slippage_bps=_as_float(raw.get("slippage_bps", 10), "backtest.slippage_bps"),
        strict=_as_bool(raw.get("strict", True), "backtest.strict"),
        quantity=_as_int(raw.get("quantity", 1), "backtest.quantity"),
        lot_size=_as_int(raw.get("lot_size", 1), "backtest.lot_size"),
        max_open_positions=_as_int(raw.get("max_open_positions", 1), "backtest.max_open_positions"),
        starting_cash=_as_float(raw.get("starting_cash", 1_000_000), "backtest.starting_cash"),
        cost_model_version=str(raw.get("cost_model_version", "costs.india.fn_o.v1")),
        slippage_model_version=str(raw.get("slippage_model_version", "slip.ask.v1")),
        train_sessions=_as_int(wf.get("train_sessions", 6), "backtest.walk_forward.train_sessions"),
        validate_sessions=_as_int(wf.get("validate_sessions", 2), "backtest.walk_forward.validate_sessions"),
        test_sessions=_as_int(wf.get("test_sessions", 2), "backtest.walk_forward.test_sessions"),
        step_sessions=_as_int(wf.get("step_sessions", 2), "backtest.walk_forward.step_sessions"),
        embargo_sessions=_as_int(wf.get("embargo_sessions", 1), "backtest.walk_forward.embargo_sessions"),
        calibrate_on_test=_as_bool(wf.get("calibrate_on_test", False), "backtest.walk_forward.calibrate_on_test"),
    )


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
    raw_strategies = g.get("strategies") or {}
    universe = market.get("universe") or []
    if not isinstance(universe, list) or not universe:
        raise GrowConfigError("grow.market.universe must be a non-empty list")

    timeframes = data.get("timeframes") or ["D1", "M15", "M5"]
    if not isinstance(timeframes, list):
        raise GrowConfigError("grow.data.timeframes must be a list")
    if not isinstance(raw_strategies, dict):
        raise GrowConfigError("grow.strategies must be a mapping")
    strategy_universe = raw_strategies.get("universe") or ["NIFTY", "BANKNIFTY"]
    if not isinstance(strategy_universe, list) or not strategy_universe:
        raise GrowConfigError("grow.strategies.universe must be a non-empty list")
    specs = _strategy_specs(raw_strategies)

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
        strategies=StrategiesConfig(
            universe=tuple(str(s).strip().upper() for s in strategy_universe),
            primary_timeframe=str(raw_strategies.get("primary_timeframe", "M15")).upper(),
            supported_timeframes=tuple(
                str(tf).strip().upper()
                for tf in (raw_strategies.get("supported_timeframes") or ["M5", "M15", "D1"])
            ),
            specs=specs,
        ),
        options=_options_config(g.get("options") or {}),
        ai=_ai_config(g.get("ai") or {}),
        backtest=_backtest_config(g.get("backtest") or {}),
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
