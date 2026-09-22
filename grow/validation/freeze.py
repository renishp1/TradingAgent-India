"""Frozen evaluation configuration. Final OOS cannot silently mutate knobs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Mapping

from grow.config import GrowConfig
from grow.errors import GrowConfigError
from grow.paper.fills import policy_from_config
from grow.validation.models import COST_MODEL, SLIP_MODEL_PAPER, WalkForwardPlan


def _digest(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def code_commit() -> str:
    try:
        from pathlib import Path

        git = Path("/workspace/.git")
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = git / head.split(" ", 1)[1].strip()
            return ref.read_text(encoding="utf-8").strip()[:40]
        return head[:40]
    except OSError:
        return "workspace"


@dataclass(frozen=True)
class FrozenEvaluationConfig:
    """Pinned knobs for one walk-forward validation run."""

    configuration_version: str
    code_commit: str
    risk_ruleset: str
    risk_limits: Mapping[str, Any]
    fee_model_version: str
    slippage_model_version: str
    fill_model: str
    entry_price_source: str
    exit_price_source: str
    slippage_bps: float
    paper_starting_cash: float
    universe: tuple[str, ...]
    train_sessions: int
    validate_sessions: int
    test_sessions: int
    step_sessions: int
    embargo_sessions: int
    calibrate_on_test: bool
    fingerprint: str

    def assert_unmodified(self, other: "FrozenEvaluationConfig") -> None:
        if self.fingerprint != other.fingerprint:
            raise GrowConfigError("EVAL_CONFIG_MUTATED")


def freeze_config(config: GrowConfig) -> FrozenEvaluationConfig:
    config.assert_safe()
    if config.execution.live_trading_enabled or config.live_data.live_trading:
        raise GrowConfigError("LIVE_TRADING_FORBIDDEN")
    if not config.live_data.paper_mode:
        raise GrowConfigError("PAPER_MODE_REQUIRED")
    if config.backtest.calibrate_on_test:
        raise GrowConfigError("TEST_WINDOW_TUNING")
    policy = policy_from_config(config)
    risk = config.risk
    limits = {
        "max_position_notional": risk.max_position_notional,
        "max_gross_notional": risk.max_gross_notional,
        "max_daily_loss": risk.max_daily_loss,
        "max_symbol_concentration": risk.max_symbol_concentration,
        "require_stop_loss": risk.require_stop_loss,
        "ruleset": risk.ruleset,
    }
    bt = config.backtest
    payload = {
        "configuration_version": config.version,
        "code_commit": code_commit(),
        "risk_ruleset": risk.ruleset,
        "risk_limits": limits,
        "fee_model_version": bt.cost_model_version or COST_MODEL,
        "slippage_model_version": policy.version if policy.deterministic else (bt.slippage_model_version or SLIP_MODEL_PAPER),
        "fill_model": policy.model,
        "entry_price_source": policy.entry_source,
        "exit_price_source": policy.exit_source,
        "slippage_bps": policy.slippage_bps,
        "paper_starting_cash": config.paper.starting_cash,
        "universe": list(config.strategies.universe),
        "train_sessions": bt.train_sessions,
        "validate_sessions": bt.validate_sessions,
        "test_sessions": bt.test_sessions,
        "step_sessions": bt.step_sessions,
        "embargo_sessions": bt.embargo_sessions,
        "calibrate_on_test": False,
    }
    fp = _digest(payload)
    return FrozenEvaluationConfig(
        configuration_version=str(payload["configuration_version"]),
        code_commit=str(payload["code_commit"]),
        risk_ruleset=str(payload["risk_ruleset"]),
        risk_limits=limits,
        fee_model_version=str(payload["fee_model_version"]),
        slippage_model_version=str(payload["slippage_model_version"]),
        fill_model=str(payload["fill_model"]),
        entry_price_source=str(payload["entry_price_source"]),
        exit_price_source=str(payload["exit_price_source"]),
        slippage_bps=float(payload["slippage_bps"]),
        paper_starting_cash=float(payload["paper_starting_cash"]),
        universe=tuple(config.strategies.universe),
        train_sessions=int(payload["train_sessions"]),
        validate_sessions=int(payload["validate_sessions"]),
        test_sessions=int(payload["test_sessions"]),
        step_sessions=int(payload["step_sessions"]),
        embargo_sessions=int(payload["embargo_sessions"]),
        calibrate_on_test=False,
        fingerprint=fp,
    )


def plan_from_config(
    config: GrowConfig,
    *,
    dataset_id: str,
    dataset_version: str,
    dataset_fingerprint: str,
    provider_name: str,
    plan_id: str = "wf-plan",
) -> WalkForwardPlan:
    frozen = freeze_config(config)
    plan = WalkForwardPlan(
        plan_id=plan_id,
        configuration_version=frozen.configuration_version,
        code_commit=frozen.code_commit,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        dataset_fingerprint=dataset_fingerprint,
        provider_name=provider_name,
        train_sessions=frozen.train_sessions,
        validate_sessions=frozen.validate_sessions,
        test_sessions=frozen.test_sessions,
        step_sessions=frozen.step_sessions,
        embargo_sessions=frozen.embargo_sessions,
        fee_model_version=frozen.fee_model_version,
        slippage_model_version=frozen.slippage_model_version,
        risk_ruleset=frozen.risk_ruleset,
        universe=frozen.universe,
        calibration_mode="NONE",
        trainable_parameters=(),
    )
    return plan.frozen()


def assert_final_eval_isolation(
    frozen: FrozenEvaluationConfig,
    config: GrowConfig,
    *,
    allow_tune: bool = False,
) -> None:
    """Final evaluation results must not silently modify the evaluated configuration."""

    if allow_tune:
        raise GrowConfigError("TEST_WINDOW_TUNING")
    current = freeze_config(config)
    frozen.assert_unmodified(current)
    if config.backtest.calibrate_on_test:
        raise GrowConfigError("TEST_WINDOW_TUNING")


def with_cost_stress(config: GrowConfig, *, slip_mult: float = 1.0) -> GrowConfig:
    """Return a replace()'d config for fee/slippage robustness — does not mutate original."""

    paper = config.paper
    bps = 0.0 if paper.fill_model == "deterministic" else float(paper.slippage_bps or config.backtest.slippage_bps)
    return replace(
        config,
        paper=replace(paper, fill_model="configurable", slippage_bps=bps * slip_mult),
        backtest=replace(config.backtest, slippage_bps=config.backtest.slippage_bps * slip_mult),
    )
