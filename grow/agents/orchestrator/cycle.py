"""CEO / Orchestrator — coordinates specialists; never bypasses Risk Guard."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from grow.agents.auditor import AuditorAgent
from grow.agents.base import SpecialistAgent
from grow.agents.market_data import MarketDataAgent
from grow.agents.options import OptionsChainAgent
from grow.agents.regime import RegimeAgent
from grow.agents.risk_guard import AgentRiskGuard, RiskGateDecision
from grow.agents.strategy_research import StrategyResearchAgent
from grow.agents.technical import TechnicalAgent
from grow.decision.aggregation.debate import DebateSummary, summarize_debate
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.journal.record import DecisionCycleRecord, JournalStore
from grow.execution.lock import assert_paper_runtime
from grow.market_data.normalized.models import AgentMarketSnapshot
from grow.market_data.snapshots.builder import gate_snapshot_quality


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
            "paper_execution_result": self.paper_execution_result,
            "record": self.record.to_dict(),
            "live_trading": False,
            "paper_mode": True,
        }


class AgentCycleOrchestrator:
    """Run one paper decision cycle over a shared immutable snapshot."""

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
        if specialists is None:
            specialists = (
                MarketDataAgent(),
                TechnicalAgent(),
                OptionsChainAgent(),
                RegimeAgent(),
                StrategyResearchAgent(configured_strategies),
            )
        self.specialists = specialists

    def run(self, snapshot: AgentMarketSnapshot) -> OrchestratorDecision:
        assert_paper_runtime("paper", False, "PAPER")
        cycle_id = f"cycle-{uuid.uuid4().hex[:12]}"
        quality = gate_snapshot_quality(snapshot)
        if quality.value in {"INSUFFICIENT", "REJECTED", "STALE"}:
            empty = ()
            debate = summarize_debate(empty)
            risk = RiskGateDecision(False, "REJECT", f"DATA_QUALITY_GATE:{quality.value}", snapshot.quality_notes)
            record = self._record(
                cycle_id,
                snapshot,
                empty,
                debate,
                orchestrator_decision="NO_TRADE",
                orchestrator_reason=f"DATA_QUALITY_GATE:{quality.value}",
                risk=risk,
                paper_execution_result="SKIPPED",
            )
            return OrchestratorDecision(
                cycle_id=cycle_id,
                snapshot_id=snapshot.snapshot_id,
                decision="NO_TRADE",
                reason=f"DATA_QUALITY_GATE:{quality.value}",
                proposed_action=CandidateAction.NONE,
                proposed_instrument=None,
                debate=debate,
                risk=risk,
                agent_results=empty,
                paper_execution_result="SKIPPED",
                record=record,
            )

        results = self._run_specialists(snapshot)
        debate = summarize_debate(results)
        if self.allow_second_pass and self.second_pass_rule is not None and self.second_pass_rule(results, debate):
            # One optional second pass only when an explicit rule permits it.
            results = self._run_specialists(snapshot)
            debate = summarize_debate(results)

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
            # Even if approved, only paper-execution may create positions — foundation skips.
            decision = "PAPER_READY"
            paper_result = "NOT_EXECUTED_FOUNDATION"
            reason = synth_reason

        record = self._record(
            cycle_id,
            snapshot,
            results,
            debate,
            orchestrator_decision=decision,
            orchestrator_reason=reason,
            risk=risk,
            paper_execution_result=paper_result,
        )
        return OrchestratorDecision(
            cycle_id=cycle_id,
            snapshot_id=snapshot.snapshot_id,
            decision=decision,
            reason=reason,
            proposed_action=proposed_action,
            proposed_instrument=proposed_instrument,
            debate=debate,
            risk=risk,
            agent_results=results,
            paper_execution_result=paper_result,
            record=record,
        )

    def _run_specialists(self, snapshot: AgentMarketSnapshot) -> tuple[AgentResult, ...]:
        out: list[AgentResult] = []
        for agent in self.specialists:
            try:
                result = agent.analyze(snapshot)
                if result.snapshot_id != snapshot.snapshot_id:
                    raise ValueError("snapshot_id_mismatch")
            except Exception as exc:  # one failed specialist must not crash the cycle
                result = AgentResult(
                    agent_name=getattr(agent, "agent_name", type(agent).__name__),
                    agent_version=getattr(agent, "agent_version", "unknown"),
                    snapshot_id=snapshot.snapshot_id,
                    decision_timestamp=snapshot.decision_timestamp,
                    status=AgentStatus.ERROR,
                    observations=(f"agent_failure:{type(exc).__name__}",),
                    metrics_used=(),
                    candidate_action=CandidateAction.NONE,
                    candidate_instrument=None,
                    entry_reason=None,
                    invalidation_reason=str(exc)[:200],
                    risk_flags=("AGENT_FAILURE",),
                    missing_data=(),
                    confidence=None,
                )
            out.append(result)
        return tuple(out)

    def _synthesize(
        self,
        results: tuple[AgentResult, ...],
        debate: DebateSummary,
    ) -> tuple[CandidateAction, str | None, str]:
        if debate.error_agents:
            return CandidateAction.NONE, None, f"SPECIALIST_ERROR:{','.join(debate.error_agents)}"
        if debate.insufficient_agents and not debate.actions:
            return CandidateAction.NONE, None, f"DATA_INSUFFICIENT:{','.join(debate.insufficient_agents)}"
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
        # Preserve independence: only accept a single unanimous actionable proposal.
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
        versions = tuple(
            (row.agent_name, row.agent_version)
            for row in results
        ) or tuple(
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
