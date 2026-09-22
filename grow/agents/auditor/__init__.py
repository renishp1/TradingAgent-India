"""Trade Journal / Auditor — records cycle inputs and outcomes."""

from __future__ import annotations

from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.journal.record import DecisionCycleRecord, JournalStore
from grow.market_data.normalized.models import AgentMarketSnapshot


class AuditorAgent:
    agent_name = "auditor"
    agent_version = "auditor.v1"

    def __init__(self, store: JournalStore | None = None) -> None:
        self.store = store or JournalStore()

    def record(self, cycle: DecisionCycleRecord) -> DecisionCycleRecord:
        self.store.append(cycle)
        return cycle

    def analyze(self, snapshot: AgentMarketSnapshot) -> AgentResult:
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=(f"journal_size={len(self.store.records)}",),
            metrics_used=("journal_size",),
            candidate_action=CandidateAction.ABSTAIN,
            candidate_instrument=None,
            entry_reason="audit readiness",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
            confidence=None,
        )
