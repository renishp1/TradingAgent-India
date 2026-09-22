"""Decision-cycle audit records. Structured memory only — no free-form agent memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult


@dataclass(frozen=True)
class DecisionCycleRecord:
    cycle_id: str
    snapshot_id: str
    input_timestamp: datetime
    agent_versions: tuple[tuple[str, str], ...]
    agent_outputs: tuple[AgentResult, ...]
    debate: DebateSummary
    orchestrator_decision: str
    orchestrator_reason: str
    risk_guard_decision: str
    risk_guard_reason: str
    paper_execution_result: str
    paper_position_id: str | None = None
    final_pnl: float | None = None
    schema_version: str = "grow.decision.cycle.v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "snapshot_id": self.snapshot_id,
            "input_timestamp": self.input_timestamp.isoformat(),
            "agent_versions": [{"agent": name, "version": version} for name, version in self.agent_versions],
            "agent_outputs": [row.to_dict() for row in self.agent_outputs],
            "disagreements": self.debate.to_dict(),
            "orchestrator_decision": self.orchestrator_decision,
            "orchestrator_reason": self.orchestrator_reason,
            "risk_guard_decision": self.risk_guard_decision,
            "risk_guard_reason": self.risk_guard_reason,
            "paper_execution_result": self.paper_execution_result,
            "paper_position_id": self.paper_position_id,
            "final_pnl": self.final_pnl,
            "schema_version": self.schema_version,
            "live_trading": False,
            "paper_mode": True,
        }


@dataclass
class JournalStore:
    """Append-only in-memory journal for decision cycles."""

    records: list[DecisionCycleRecord] = field(default_factory=list)

    def append(self, record: DecisionCycleRecord) -> None:
        self.records.append(record)

    def to_list(self) -> list[dict[str, Any]]:
        return [row.to_dict() for row in self.records]
