"""CEO / Orchestrator — coordinates specialists; never bypasses Risk Guard."""

from __future__ import annotations

import hashlib
from concurrent.futures import Future, ThreadPoolExecutor, wait
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
    """Run one paper decision cycle over a shared immutable snapshot.

    Specialists consume the same ``AgentMarketSnapshot`` concurrently. Each
    specialist is bounded by ``agent_timeout_seconds`` wall-clock time so one
    slow agent cannot hang the cycle.

    Second pass is **opt-in** (``allow_second_pass=False`` by default). A future
    second pass must supply explicit context — for example first-pass conflict
    → targeted re-evaluation → same immutable snapshot → conflict context to
    the agents. Simply re-running the same agents twice without a reason is not
    a valid second-pass policy.
    """

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
        if agent_timeout_seconds <= 0:
            raise ValueError("agent_timeout_seconds must be positive")
        self.risk_guard = risk_guard or AgentRiskGuard()
        self.auditor = auditor or AuditorAgent()
        self.journal: JournalStore = self.auditor.store
        self.agent_timeout_seconds = agent_timeout_seconds
        # Opt-in only. Default remains disabled; see class docstring.
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
        cycle_id = stable_cycle_id(snapshot.snapshot_id, snapshot.decision_timestamp)
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
        # Second pass stays opt-in and must be justified by second_pass_rule.
        # Future work must pass conflict context into a targeted re-evaluation
        # of the same immutable snapshot — not a blind identical re-run.
        if (
            self.allow_second_pass
            and self.second_pass_rule is not None
            and self.second_pass_rule(results, debate)
        ):
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
        """Run specialists concurrently with a shared per-cycle timeout budget.

        Result tuple order matches ``self.specialists`` registration order.
        Timed-out specialists become ERROR with risk flag AGENT_TIMEOUT.
        """
        if not self.specialists:
            return ()

        workers = len(self.specialists)
        out: list[AgentResult | None] = [None] * workers
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            future_to_index: dict[Future[AgentResult], int] = {}
            for index, agent in enumerate(self.specialists):
                future = pool.submit(self._analyze_one, agent, snapshot)
                future_to_index[future] = index

            done, pending = wait(
                future_to_index.keys(),
                timeout=self.agent_timeout_seconds,
            )
            for future in done:
                index = future_to_index[future]
                agent = self.specialists[index]
                out[index] = self._result_from_future(future, agent, snapshot)
            for future in pending:
                index = future_to_index[future]
                agent = self.specialists[index]
                future.cancel()
                out[index] = self._timeout_result(agent, snapshot)
        finally:
            # Do not wait for timed-out workers; the cycle must return promptly.
            pool.shutdown(wait=False, cancel_futures=True)

        return tuple(out)  # type: ignore[arg-type]

    def _analyze_one(self, agent: SpecialistAgent, snapshot: AgentMarketSnapshot) -> AgentResult:
        result = agent.analyze(snapshot)
        if result.snapshot_id != snapshot.snapshot_id:
            raise ValueError("snapshot_id_mismatch")
        return result

    def _result_from_future(
        self,
        future: Future[AgentResult],
        agent: SpecialistAgent,
        snapshot: AgentMarketSnapshot,
    ) -> AgentResult:
        exc = future.exception()
        if exc is not None:
            return self._error_result(agent, snapshot, exc, risk_flag="AGENT_FAILURE")
        result = future.result()
        if result.snapshot_id != snapshot.snapshot_id:
            return self._error_result(
                agent,
                snapshot,
                ValueError("snapshot_id_mismatch"),
                risk_flag="AGENT_FAILURE",
            )
        return result

    def _timeout_result(self, agent: SpecialistAgent, snapshot: AgentMarketSnapshot) -> AgentResult:
        return AgentResult(
            agent_name=getattr(agent, "agent_name", type(agent).__name__),
            agent_version=getattr(agent, "agent_version", "unknown"),
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.ERROR,
            observations=(
                f"agent_timeout:{self.agent_timeout_seconds}s",
                "fail_closed: timed-out specialist cannot contribute a recommendation",
            ),
            metrics_used=(),
            candidate_action=CandidateAction.NONE,
            candidate_instrument=None,
            entry_reason=None,
            invalidation_reason=f"specialist exceeded {self.agent_timeout_seconds}s timeout",
            risk_flags=("AGENT_TIMEOUT",),
            missing_data=(),
            confidence=None,
        )

    def _error_result(
        self,
        agent: SpecialistAgent,
        snapshot: AgentMarketSnapshot,
        exc: BaseException,
        *,
        risk_flag: str,
    ) -> AgentResult:
        return AgentResult(
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
            risk_flags=(risk_flag,),
            missing_data=(),
            confidence=None,
        )

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
    """Deterministic cycle ID from snapshot identity and decision timestamp."""
    raw = f"{snapshot_id}:{as_of.isoformat()}"
    return "cycle-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
