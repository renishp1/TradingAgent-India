"""Machine-readable specialist-agent output contract (Requirement 4B)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


AGENT_RESULT_SCHEMA = "grow.agent.result.v2"


class AgentStatus(str, Enum):
    """Specialist status values for Requirement 4B."""

    PASS = "PASS"
    DEGRADED = "DEGRADED"
    NO_DATA = "NO_DATA"
    ERROR = "ERROR"


class CandidateAction(str, Enum):
    """Research recommendation only — never an execution command."""

    NONE = "NONE"
    PAPER_OPEN = "PAPER_OPEN"
    PAPER_CLOSE = "PAPER_CLOSE"
    HOLD = "HOLD"
    ABSTAIN = "ABSTAIN"


class StatementKind(str, Enum):
    """Fact vs interpretation separation (4B §8)."""

    FACT = "FACT"
    CALCULATED = "CALCULATED"
    INTERPRETATION = "INTERPRETATION"
    ASSUMPTION = "ASSUMPTION"
    FINDING = "FINDING"


@dataclass(frozen=True)
class AgentResult:
    """Structured specialist output. Source of truth for orchestration."""

    agent_name: str
    agent_version: str
    snapshot_id: str
    snapshot_version: str
    decision_timestamp: datetime
    status: AgentStatus
    observations: tuple[str, ...]
    calculated_metrics: Mapping[str, Any]
    interpretation: tuple[str, ...]
    findings: tuple[str, ...]
    data_quality_concerns: tuple[str, ...]
    assumptions: tuple[str, ...]
    evidence: tuple[str, ...]
    metrics_used: tuple[str, ...]
    candidate_action: CandidateAction
    candidate_instrument: str | None
    entry_reason: str | None
    invalidation_reason: str | None
    risk_flags: tuple[str, ...]
    missing_data: tuple[str, ...]
    confidence: float | None = None
    cycle_id: str = ""
    execution_time_ms: float | None = None
    schema_version: str = AGENT_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != AGENT_RESULT_SCHEMA:
            raise ValueError("unsupported agent result schema")
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be in [0, 1] when configured")
        if self.status is AgentStatus.NO_DATA and not self.missing_data:
            raise ValueError("NO_DATA requires missing_data")
        object.__setattr__(
            self,
            "calculated_metrics",
            MappingProxyType(dict(self.calculated_metrics)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "cycle_id": self.cycle_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "decision_timestamp": self.decision_timestamp.isoformat(),
            "status": self.status.value,
            "observations": list(self.observations),
            "calculated_metrics": dict(self.calculated_metrics),
            "interpretation": list(self.interpretation),
            "findings": list(self.findings),
            "data_quality_concerns": list(self.data_quality_concerns),
            "assumptions": list(self.assumptions),
            "evidence": list(self.evidence),
            "metrics_used": list(self.metrics_used),
            "candidate_action": self.candidate_action.value,
            "candidate_instrument": self.candidate_instrument,
            "entry_reason": self.entry_reason,
            "invalidation_reason": self.invalidation_reason,
            "risk_flags": list(self.risk_flags),
            "missing_data": list(self.missing_data),
            "confidence": self.confidence,
            "execution_time_ms": self.execution_time_ms,
            "schema_version": self.schema_version,
            "live_trading": False,
            "executed": False,
        }


@dataclass(frozen=True)
class AgentInput:
    """Common specialist input contract (4B §7.1)."""

    cycle_id: str
    snapshot: Any  # AgentMarketSnapshot — typed loosely to avoid circular imports
    agent_config_version: str = ""

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id

    @property
    def snapshot_version(self) -> str:
        return self.snapshot.version

    @property
    def as_of(self) -> datetime:
        return self.snapshot.decision_timestamp
