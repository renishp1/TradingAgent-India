"""Evaluation run contracts, frozen plans, and window-role results."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping

from grow.validation.windows import WalkWindow

EVAL_SCHEMA = "walkforward.evaluation.v1"
PLAN_SCHEMA = "walkforward.plan.v1"
COST_MODEL = "costs.india.fn_o.v1"
SLIP_MODEL_PAPER = "paper.slip.v1"
LABEL = "WALK-FORWARD VALIDATION / NOT LIVE"


def _digest(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WalkForwardPlan:
    """Frozen evaluation protocol. Final OOS config must not change after freeze."""

    plan_id: str
    configuration_version: str
    code_commit: str
    dataset_id: str
    dataset_version: str
    dataset_fingerprint: str
    provider_name: str
    train_sessions: int
    validate_sessions: int
    test_sessions: int
    step_sessions: int
    embargo_sessions: int
    fee_model_version: str
    slippage_model_version: str
    risk_ruleset: str
    universe: tuple[str, ...]
    calibration_mode: str = "NONE"
    trainable_parameters: tuple[str, ...] = ()
    freeze_hash: str | None = None

    def freeze_payload(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "configuration_version": self.configuration_version,
            "code_commit": self.code_commit,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_fingerprint": self.dataset_fingerprint,
            "provider_name": self.provider_name,
            "train_sessions": self.train_sessions,
            "validate_sessions": self.validate_sessions,
            "test_sessions": self.test_sessions,
            "step_sessions": self.step_sessions,
            "embargo_sessions": self.embargo_sessions,
            "fee_model_version": self.fee_model_version,
            "slippage_model_version": self.slippage_model_version,
            "risk_ruleset": self.risk_ruleset,
            "universe": list(self.universe),
            "calibration_mode": self.calibration_mode,
            "trainable_parameters": list(self.trainable_parameters),
            "schema": PLAN_SCHEMA,
        }

    def fingerprint(self) -> str:
        return _digest(self.freeze_payload())

    def frozen(self) -> "WalkForwardPlan":
        if self.calibration_mode != "NONE":
            from grow.errors import GrowConfigError

            raise GrowConfigError("TEST_WINDOW_TUNING")
        if self.trainable_parameters:
            from grow.errors import GrowConfigError

            raise GrowConfigError("TRAINABLE_PARAMETERS_FORBIDDEN")
        return WalkForwardPlan(**{**self.__dict__, "freeze_hash": self.fingerprint()})

    def to_dict(self) -> dict[str, Any]:
        body = self.freeze_payload()
        body["freeze_hash"] = self.freeze_hash
        body["live"] = False
        body["label"] = LABEL
        return body


@dataclass(frozen=True)
class EvaluationRun:
    """Pinned evaluation run contract (requirement §7)."""

    run_id: str
    configuration_version: str
    code_commit: str
    dataset_id: str
    dataset_version: str
    dataset_fingerprint: str
    provider_name: str
    start: date
    end: date
    train: tuple[date, date] | None
    validate: tuple[date, date] | None
    test: tuple[date, date]
    strategy_agent_config: Mapping[str, Any]
    universe: tuple[str, ...]
    fee_model_version: str
    slippage_model_version: str
    risk_limit_config: Mapping[str, Any]
    cycle_count: int
    candidate_count: int
    trade_count: int
    rejection_count: int
    metrics: Mapping[str, Any]
    artifact_refs: Mapping[str, str]
    window_role: str
    window_index: int
    leakage_status: str
    created_at: str
    plan_freeze_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "configuration_version": self.configuration_version,
            "code_commit": self.code_commit,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_fingerprint": self.dataset_fingerprint,
            "provider_name": self.provider_name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "train": None if self.train is None else [self.train[0].isoformat(), self.train[1].isoformat()],
            "validate": None
            if self.validate is None
            else [self.validate[0].isoformat(), self.validate[1].isoformat()],
            "test": [self.test[0].isoformat(), self.test[1].isoformat()],
            "strategy_agent_config": dict(self.strategy_agent_config),
            "universe": list(self.universe),
            "fee_model_version": self.fee_model_version,
            "slippage_model_version": self.slippage_model_version,
            "risk_limit_config": dict(self.risk_limit_config),
            "cycle_count": self.cycle_count,
            "candidate_count": self.candidate_count,
            "trade_count": self.trade_count,
            "rejection_count": self.rejection_count,
            "metrics": dict(self.metrics),
            "artifact_refs": dict(self.artifact_refs),
            "window_role": self.window_role,
            "window_index": self.window_index,
            "leakage_status": self.leakage_status,
            "created_at": self.created_at,
            "plan_freeze_hash": self.plan_freeze_hash,
            "schema": EVAL_SCHEMA,
            "live": False,
            "label": LABEL,
        }


@dataclass
class WindowRoleResult:
    role: str
    window: WalkWindow
    metrics: dict[str, Any]
    trades: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    leakage_flags: list[str] = field(default_factory=list)
    cycle_count: int = 0
    candidate_count: int = 0
    run: EvaluationRun | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "window": self.window.to_dict(),
            "metrics": self.metrics,
            "trades": list(self.trades),
            "decisions": list(self.decisions),
            "rejections": list(self.rejections),
            "leakage_flags": list(self.leakage_flags),
            "cycle_count": self.cycle_count,
            "candidate_count": self.candidate_count,
            "run": None if self.run is None else self.run.to_dict(),
        }


def make_run_id(parts: Mapping[str, Any]) -> str:
    return _digest({"kind": "walkforward.run", **parts})[:16]
