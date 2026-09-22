"""4C decision contract. Paper-trade candidate, NO_TRADE, or BLOCKED.

Status values are the existing project outcomes plus the 4C safety block:
NO_TRADE and CANDIDATE already exist on ``grow.options.models.DecisionStatus``.
BLOCKED is the Risk Guard rejection outcome required by 4C. CANDIDATE is a
paper-trade candidate (the requirement's TRADE_CANDIDATE). It is not an order.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


DECISION_SCHEMA = "grow.decision.integration.v1"

REQUIRED_DECISION_FIELDS = (
    "decision_id",
    "analysis_cycle_id",
    "snapshot_id",
    "snapshot_version",
    "decision_timestamp",
    "as_of",
    "agent_output_refs",
    "candidate_strategy",
    "candidate_instrument",
    "direction",
    "observations",
    "calculated_evidence",
    "supporting_findings",
    "conflicting_findings",
    "status",
    "reason_codes",
    "risk_guard_result",
    "risk_guard_reason",
    "data_quality_status",
    "assumptions",
    "configuration_version",
    "ruleset",
    "audit_references",
    "gate_results",
    "paper_mode",
    "live_trading",
    "broker_order_path",
    "executed",
    "schema_version",
)


class IntegratedDecisionStatus(str, Enum):
    """Final 4C outcomes. No other status is produced."""

    NO_TRADE = "NO_TRADE"
    CANDIDATE = "CANDIDATE"
    BLOCKED = "BLOCKED"


def digest_payload(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def plain_data(value: Any) -> Any:
    """JSON-stable copy. Mappings stay mappings; tuples become lists."""
    if isinstance(value, Mapping):
        return {str(key): plain_data(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain_data(item) for item in value]
    if isinstance(value, list):
        return [plain_data(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


@dataclass(frozen=True)
class DecisionBookState:
    """Book context supplied to Risk Guard. Not a limit change."""

    cash: float
    gross_notional: float
    daily_pnl: float
    symbol_notional: float
    open_positions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cash": self.cash,
            "gross_notional": self.gross_notional,
            "daily_pnl": self.daily_pnl,
            "symbol_notional": self.symbol_notional,
            "open_positions": self.open_positions,
        }


@dataclass(frozen=True)
class AgentOutputRef:
    agent_name: str
    agent_version: str
    cycle_id: str
    snapshot_id: str
    snapshot_version: str
    status: str
    relied_upon: bool
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "cycle_id": self.cycle_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "status": self.status,
            "relied_upon": self.relied_upon,
            "rejection_reason": self.rejection_reason,
        }


@dataclass(frozen=True)
class StrategyCandidate:
    """Structurally complete paper candidate. Risk Guard has not seen it yet."""

    strategy: str
    instrument: str
    underlying: str
    direction: str
    limit_price: float
    stop_loss: float
    quantity: int
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "instrument": self.instrument,
            "underlying": self.underlying,
            "direction": self.direction,
            "limit_price": self.limit_price,
            "stop_loss": self.stop_loss,
            "quantity": self.quantity,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class IntegratedDecision:
    decision_id: str
    analysis_cycle_id: str
    snapshot_id: str
    snapshot_version: str
    decision_timestamp: datetime
    as_of: datetime
    agent_output_refs: tuple[AgentOutputRef, ...]
    candidate_strategy: str | None
    candidate_instrument: str | None
    direction: str | None
    observations: tuple[str, ...]
    calculated_evidence: Mapping[str, Any]
    supporting_findings: tuple[str, ...]
    conflicting_findings: tuple[str, ...]
    status: IntegratedDecisionStatus
    reason_codes: tuple[str, ...]
    risk_guard_result: str
    risk_guard_reason: str
    data_quality_status: str
    assumptions: tuple[str, ...]
    configuration_version: str
    ruleset: str
    audit_references: tuple[str, ...]
    gate_results: tuple[tuple[str, bool, str], ...]
    risk_rule_results: tuple[tuple[str, bool, str], ...] = ()
    schema_version: str = DECISION_SCHEMA
    paper_mode: bool = True
    live_trading: bool = False
    broker_order_path: bool = False
    executed: bool = False

    def __post_init__(self) -> None:
        if self.status not in set(IntegratedDecisionStatus):
            raise ValueError("unsupported decision status")
        if self.schema_version != DECISION_SCHEMA:
            raise ValueError("unsupported decision schema")
        if self.live_trading or self.broker_order_path or self.executed or not self.paper_mode:
            raise ValueError("4C decisions stay paper-only and unexecuted")
        object.__setattr__(self, "calculated_evidence", MappingProxyType(dict(self.calculated_evidence)))

    @property
    def paper_trade_candidate(self) -> bool:
        """True only for a Risk Guard-approved paper candidate. Not an order."""
        return self.status is IntegratedDecisionStatus.CANDIDATE

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "decision_id": self.decision_id,
            "analysis_cycle_id": self.analysis_cycle_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "decision_timestamp": self.decision_timestamp.isoformat(),
            "as_of": self.as_of.isoformat(),
            "agent_output_refs": [row.to_dict() for row in self.agent_output_refs],
            "candidate_strategy": self.candidate_strategy,
            "candidate_instrument": self.candidate_instrument,
            "direction": self.direction,
            "observations": list(self.observations),
            "calculated_evidence": plain_data(self.calculated_evidence),
            "supporting_findings": list(self.supporting_findings),
            "conflicting_findings": list(self.conflicting_findings),
            "status": self.status.value,
            "paper_trade_candidate": self.paper_trade_candidate,
            "reason_codes": list(self.reason_codes),
            "risk_guard_result": self.risk_guard_result,
            "risk_guard_reason": self.risk_guard_reason,
            "risk_rule_results": [
                {"id": rid, "passed": passed, "detail": detail}
                for rid, passed, detail in self.risk_rule_results
            ],
            "data_quality_status": self.data_quality_status,
            "assumptions": list(self.assumptions),
            "configuration_version": self.configuration_version,
            "ruleset": self.ruleset,
            "audit_references": list(self.audit_references),
            "gate_results": [
                {"gate": gate, "passed": passed, "detail": detail}
                for gate, passed, detail in self.gate_results
            ],
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
            "executed": False,
            "schema_version": self.schema_version,
        }
        missing = [key for key in REQUIRED_DECISION_FIELDS if key not in payload]
        if missing:
            raise ValueError(f"decision contract missing {missing}")
        return payload
