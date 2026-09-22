"""4B orchestration models — analysis cycle and aggregate package."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult


ANALYSIS_PACKAGE_SCHEMA = "grow.orchestration.analysis_package.v1"


@dataclass(frozen=True)
class AgentDispatchRecord:
    agent_name: str
    agent_version: str
    status: str
    started_at: datetime
    ended_at: datetime
    accepted: bool
    rejection_reason: str | None = None
    execution_time_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "accepted": self.accepted,
            "rejection_reason": self.rejection_reason,
            "execution_time_ms": self.execution_time_ms,
        }


@dataclass(frozen=True)
class AggregateAnalysisPackage:
    """Auditable multi-agent analysis package for Requirement 4C handoff."""

    cycle_id: str
    snapshot_id: str
    snapshot_version: str
    as_of: datetime
    agent_outputs: tuple[AgentResult, ...]
    rejected_outputs: tuple[dict[str, Any], ...]
    dispatch_records: tuple[AgentDispatchRecord, ...]
    conflicts: tuple[str, ...]
    supporting_evidence: tuple[str, ...]
    conflicting_evidence: tuple[str, ...]
    unavailable_agents: tuple[str, ...]
    debate: DebateSummary
    cycle_summary: str
    package_digest: str
    schema_version: str = ANALYSIS_PACKAGE_SCHEMA
    paper_mode: bool = True
    live_trading: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "as_of": self.as_of.isoformat(),
            "agent_outputs": [row.to_dict() for row in self.agent_outputs],
            "rejected_outputs": list(self.rejected_outputs),
            "dispatch_records": [row.to_dict() for row in self.dispatch_records],
            "conflicts": list(self.conflicts),
            "supporting_evidence": list(self.supporting_evidence),
            "conflicting_evidence": list(self.conflicting_evidence),
            "unavailable_agents": list(self.unavailable_agents),
            "debate": self.debate.to_dict(),
            "cycle_summary": self.cycle_summary,
            "package_digest": self.package_digest,
            "schema_version": self.schema_version,
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
        }
