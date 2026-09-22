"""Machine-readable specialist-agent output contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


AGENT_RESULT_SCHEMA = "grow.agent.result.v1"


class AgentStatus(str, Enum):
    PASS = "PASS"
    NO_TRADE = "NO_TRADE"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    REJECT = "REJECT"
    ERROR = "ERROR"


class CandidateAction(str, Enum):
    NONE = "NONE"
    PAPER_OPEN = "PAPER_OPEN"
    PAPER_CLOSE = "PAPER_CLOSE"
    HOLD = "HOLD"
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True)
class AgentResult:
    agent_name: str
    agent_version: str
    snapshot_id: str
    decision_timestamp: datetime
    status: AgentStatus
    observations: tuple[str, ...]
    metrics_used: tuple[str, ...]
    candidate_action: CandidateAction
    candidate_instrument: str | None
    entry_reason: str | None
    invalidation_reason: str | None
    risk_flags: tuple[str, ...]
    missing_data: tuple[str, ...]
    confidence: float | None = None
    schema_version: str = AGENT_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != AGENT_RESULT_SCHEMA:
            raise ValueError("unsupported agent result schema")
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be in [0, 1] when configured")
        if self.status is AgentStatus.DATA_INSUFFICIENT and not self.missing_data:
            raise ValueError("DATA_INSUFFICIENT requires missing_data")

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "snapshot_id": self.snapshot_id,
            "decision_timestamp": self.decision_timestamp.isoformat(),
            "status": self.status.value,
            "observations": list(self.observations),
            "metrics_used": list(self.metrics_used),
            "candidate_action": self.candidate_action.value,
            "candidate_instrument": self.candidate_instrument,
            "entry_reason": self.entry_reason,
            "invalidation_reason": self.invalidation_reason,
            "risk_flags": list(self.risk_flags),
            "missing_data": list(self.missing_data),
            "confidence": self.confidence,
            "schema_version": self.schema_version,
            "live_trading": False,
            "executed": False,
        }
