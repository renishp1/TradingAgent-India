"""2E contracts. Historical research only. Not live fills. Not PaperLedger."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

MANIFEST_SCHEMA = "backtest.manifest.v1"
TRADE_SCHEMA = "backtest.trade.v1"
RESULT_SCHEMA = "backtest.result.v1"
COST_MODEL = "costs.india.fn_o.v1"
SLIP_MODEL = "slip.ask.v1"
EXEC_MODEL = "backtest.exec.v1"


def _digest(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BacktestRunManifest:
    run_id: str
    dataset_id: str
    dataset_version: str
    calendar_version: str
    code_commit: str
    config_version: str
    strategy_version: str
    options_engine_version: str
    research_schema_version: str
    prompt_versions: Mapping[str, str]
    model_provider_mode: str
    cost_model_version: str
    slippage_model_version: str
    execution_version: str
    evaluation_start: str
    evaluation_end: str
    timezone: str
    random_seed: str | None
    created_at: str
    strictness_mode: str
    fill_model: str
    ablation: str
    fingerprint: str
    mapping_policy: str = "EXACT"
    slot_tolerance_seconds: int = 0
    dataset_fingerprint: str = ""
    provider_name: str = "fixture"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "calendar_version": self.calendar_version,
            "code_commit": self.code_commit,
            "config_version": self.config_version,
            "strategy_version": self.strategy_version,
            "options_engine_version": self.options_engine_version,
            "research_schema_version": self.research_schema_version,
            "prompt_versions": dict(self.prompt_versions),
            "model_provider_mode": self.model_provider_mode,
            "cost_model_version": self.cost_model_version,
            "slippage_model_version": self.slippage_model_version,
            "execution_version": self.execution_version,
            "evaluation_start": self.evaluation_start,
            "evaluation_end": self.evaluation_end,
            "timezone": self.timezone,
            "random_seed": self.random_seed,
            "created_at": self.created_at,
            "strictness_mode": self.strictness_mode,
            "fill_model": self.fill_model,
            "ablation": self.ablation,
            "fingerprint": self.fingerprint,
            "mapping_policy": self.mapping_policy,
            "slot_tolerance_seconds": self.slot_tolerance_seconds,
            "dataset_fingerprint": self.dataset_fingerprint,
            "provider_name": self.provider_name,
            "schema": MANIFEST_SCHEMA,
            "live": False,
        }


@dataclass(frozen=True)
class SimulatedFill:
    side: str
    price: float
    quantity: int
    slippage: float
    timestamp: datetime
    reference: str
    reason: str


@dataclass(frozen=True)
class BacktestTrade:
    trade_id: str
    run_id: str
    underlying: str
    decision_as_of: datetime
    strategy_signal_id: str
    strategy_version: str
    direction: str
    option_candidate_id: str
    expiry: str
    strike: float
    option_type: str
    entry_reference: float
    entry_fill: float
    quantity: int
    lot_size: int
    exit_reason: str
    exit_timestamp: datetime
    exit_fill: float
    gross_pnl: float
    total_cost: float
    net_pnl: float
    data_quality_flags: tuple[str, ...]
    research_decision_id: str
    packet_id: str
    source_snapshot_ids: Mapping[str, str]
    primary: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "run_id": self.run_id,
            "underlying": self.underlying,
            "decision_as_of": self.decision_as_of.isoformat(),
            "strategy_signal_id": self.strategy_signal_id,
            "strategy_version": self.strategy_version,
            "direction": self.direction,
            "option_candidate_id": self.option_candidate_id,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "intent": "BUY",
            "entry_reference": self.entry_reference,
            "entry_fill": self.entry_fill,
            "quantity": self.quantity,
            "lots": self.quantity,
            "lot_size": self.lot_size,
            "exit_reason": self.exit_reason,
            "exit_timestamp": self.exit_timestamp.isoformat(),
            "exit_fill": self.exit_fill,
            "gross_pnl": self.gross_pnl,
            "total_cost": self.total_cost,
            "net_pnl": self.net_pnl,
            "data_quality_flags": list(self.data_quality_flags),
            "research_decision_id": self.research_decision_id,
            "packet_id": self.packet_id,
            "source_snapshot_ids": dict(self.source_snapshot_ids),
            "primary": self.primary,
            "schema": TRADE_SCHEMA,
            "live": False,
        }


@dataclass(frozen=True)
class DecisionRow:
    as_of: datetime
    underlying: str
    status: str
    reason: str
    signal_id: str | None
    candidate_id: str | None
    decision_id: str | None
    ablation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "underlying": self.underlying,
            "status": self.status,
            "reason": self.reason,
            "signal_id": self.signal_id,
            "candidate_id": self.candidate_id,
            "decision_id": self.decision_id,
            "ablation": self.ablation,
        }


def fingerprint_for(parts: Mapping[str, Any]) -> str:
    return _digest({"kind": "backtest.fingerprint", **parts})
