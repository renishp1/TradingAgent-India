"""CEO / Orchestrator — coordinates 4B analysis then applies paper-only risk gate.

Requirement 4B produces the AggregateAnalysisPackage via AnalysisOrchestrator
(dispatch with timeouts, validation, conflict-preserving aggregation). Risk Guard
evaluation here is a safety consumer of that package (not 4C decision integration
/ paper fills).

Second pass is **opt-in** (``allow_second_pass=False`` by default). A future
second pass must supply explicit context — for example first-pass conflict →
targeted re-evaluation → same immutable snapshot → conflict context to the
agents. Simply re-running the same agents twice without a reason is not a valid
second-pass policy.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from grow.agents.auditor import AuditorAgent
from grow.agents.base import SpecialistAgent
from grow.agents.risk_guard import AgentRiskGuard, RiskGateDecision
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.journal.record import DecisionCycleRecord, JournalStore
from grow.execution.lock import assert_paper_runtime
from grow.market_data.normalized.models import AgentMarketSnapshot
from grow.orchestration.cycle import AnalysisOrchestrator, stable_cycle_id as analysis_stable_cycle_id
from grow.orchestration.models import AggregateAnalysisPackage


@dataclass(frozen=True)
class OrchestratorDecision:
    cycle_id: str
    snapshot_id: str
    decision: str
    reason: str
    proposed_action: CandidateAction
    proposed_instrument: str | None
    debate: DebateSummary
    risk: RiskGateDecision
    agent_results: tuple[AgentResult, ...]
    analysis_package: AggregateAnalysisPackage
    paper_execution_result: str
    record: DecisionCycleRecord

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "snapshot_id": self.snapshot_id,
            "decision": self.decision,
            "reason": self.reason,
            "proposed_action": self.proposed_action.value,
            "proposed_instrument": self.proposed_instrument,
            "debate": self.debate.to_dict(),
            "risk": self.risk.to_dict(),
            "agent_results": [row.to_dict() for row in self.agent_results],
            "analysis_package": self.analysis_package.to_dict(),
            "paper_execution_result": self.paper_execution_result,
            "record": self.record.to_dict(),
            "live_trading": False,
            "paper_mode": True,
            "broker_order_path": False,
        }


class AgentCycleOrchestrator:
    """Run one paper analysis+safety cycle over a shared immutable snapshot."""

    def __init__(
        self,
        *,
        specialists: tuple[SpecialistAgent, ...] | None = None,
        risk_guard: AgentRiskGuard | None = None,
        auditor: AuditorAgent | None = None,
        configured_strategies: tuple[str, ...] = (),
        agent_timeout_seconds: float = 2.0,
        allow_second_pass: bool = False,
        second_pass_rule: Callable[[tuple[AgentResult, ...], DebateSummary], bool] | None = None,
        execution_mode: str = "paper",
        live_trading: bool = False,
    ) -> None:
        assert_paper_runtime(execution_mode, live_trading, "PAPER")
        self.risk_guard = risk_guard or AgentRiskGuard()
        self.auditor = auditor or AuditorAgent()
        self.journal: JournalStore = self.auditor.store
        self.agent_timeout_seconds = agent_timeout_seconds
        self.allow_second_pass = allow_second_pass
        self.second_pass_rule = second_pass_rule
        self.analysis = AnalysisOrchestrator(
            specialists=specialists,
            configured_strategies=configured_strategies,
            agent_timeout_seconds=agent_timeout_seconds,
            execution_mode=execution_mode,
            live_trading=live_trading,
        )
        self.specialists = self.analysis.specialists

    def run(self, snapshot: AgentMarketSnapshot) -> OrchestratorDecision:
        assert_paper_runtime("paper", False, "PAPER")
        cycle_id = analysis_stable_cycle_id(snapshot.snapshot_id, snapshot.decision_timestamp)
        package = self.analysis.run(snapshot, cycle_id=cycle_id)
        results = package.agent_outputs
        debate = package.debate

        if self.allow_second_pass and self.second_pass_rule is not None and self.second_pass_rule(
            results, debate
        ):
            package = self.analysis.run(snapshot, cycle_id=f"{cycle_id}-pass2")
            results = package.agent_outputs
            debate = package.debate

        if package.cycle_summary.startswith("STOPPED_BEFORE_DISPATCH"):
            risk = RiskGateDecision(
                False,
                "REJECT",
                f"DATA_QUALITY_GATE:{snapshot.data_quality.value}",
                snapshot.quality_notes,
            )
            record = self._record(
                package.cycle_id,
                snapshot,
                results,
                debate,
                orchestrator_decision="NO_TRADE",
                orchestrator_reason=package.cycle_summary,
                risk=risk,
                paper_execution_result="SKIPPED",
            )
            return OrchestratorDecision(
                cycle_id=package.cycle_id,
                snapshot_id=snapshot.snapshot_id,
                decision="NO_TRADE",
                reason=package.cycle_summary,
                proposed_action=CandidateAction.NONE,
                proposed_instrument=None,
                debate=debate,
                risk=risk,
                agent_results=results,
                analysis_package=package,
                paper_execution_result="SKIPPED",
                record=record,
            )

        proposed_action, proposed_instrument, synth_reason = self._synthesize(results, debate)
        risk = self.risk_guard.evaluate(
            snapshot,
            proposed_action=proposed_action,
            proposed_instrument=proposed_instrument,
            specialist_results=results,
            consensus=debate.agreement,
        )
        if not risk.approved:
            decision = "NO_TRADE" if risk.decision == "NO_TRADE" else "REJECTED_BY_RISK"
            paper_result = "BLOCKED_BY_RISK"
            reason = risk.reason
        else:
            decision = "ANALYSIS_COMPLETE"
            paper_result = "NOT_EXECUTED_4B"
            reason = synth_reason

        record = self._record(
            package.cycle_id,
            snapshot,
            results,
            debate,
            orchestrator_decision=decision,
            orchestrator_reason=reason,
            risk=risk,
            paper_execution_result=paper_result,
        )
        return OrchestratorDecision(
            cycle_id=package.cycle_id,
            snapshot_id=snapshot.snapshot_id,
            decision=decision,
            reason=reason,
            proposed_action=proposed_action,
            proposed_instrument=proposed_instrument,
            debate=debate,
            risk=risk,
            agent_results=results,
            analysis_package=package,
            paper_execution_result=paper_result,
            record=record,
        )

    def _synthesize(
        self,
        results: tuple[AgentResult, ...],
        debate: DebateSummary,
    ) -> tuple[CandidateAction, str | None, str]:
        if debate.error_agents:
            return CandidateAction.NONE, None, f"SPECIALIST_ERROR:{','.join(debate.error_agents)}"
        if debate.insufficient_agents and not debate.actions:
            return CandidateAction.NONE, None, f"NO_DATA:{','.join(debate.insufficient_agents)}"
        if debate.conflicts:
            return CandidateAction.NONE, None, f"CONFLICT:{','.join(debate.conflicts)}"
        actionable = [
            row
            for row in results
            if row.status is AgentStatus.PASS
            and row.candidate_action
            in {CandidateAction.PAPER_OPEN, CandidateAction.PAPER_CLOSE}
        ]
        if not actionable:
            return CandidateAction.NONE, None, "NO_ACTIONABLE_RECOMMENDATION"
        actions = {row.candidate_action for row in actionable}
        instruments = {row.candidate_instrument for row in actionable}
        if len(actions) != 1 or len(instruments) != 1:
            return CandidateAction.NONE, None, "NON_UNANIMOUS_ACTION"
        row = actionable[0]
        return row.candidate_action, row.candidate_instrument, "UNANIMOUS_SPECIALIST_ACTION"

    def _record(
        self,
        cycle_id: str,
        snapshot: AgentMarketSnapshot,
        results: tuple[AgentResult, ...],
        debate: DebateSummary,
        *,
        orchestrator_decision: str,
        orchestrator_reason: str,
        risk: RiskGateDecision,
        paper_execution_result: str,
    ) -> DecisionCycleRecord:
        versions = tuple((row.agent_name, row.agent_version) for row in results) or tuple(
            (getattr(agent, "agent_name", "agent"), getattr(agent, "agent_version", "unknown"))
            for agent in self.specialists
        )
        record = DecisionCycleRecord(
            cycle_id=cycle_id,
            snapshot_id=snapshot.snapshot_id,
            input_timestamp=snapshot.decision_timestamp,
            agent_versions=versions,
            agent_outputs=results,
            debate=debate,
            orchestrator_decision=orchestrator_decision,
            orchestrator_reason=orchestrator_reason,
            risk_guard_decision=risk.decision,
            risk_guard_reason=risk.reason,
            paper_execution_result=paper_execution_result,
        )
        self.auditor.record(record)
        return record


def stable_cycle_id(snapshot_id: str, as_of: datetime) -> str:
    raw = f"{snapshot_id}:{as_of.isoformat()}"
    return "cycle-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
