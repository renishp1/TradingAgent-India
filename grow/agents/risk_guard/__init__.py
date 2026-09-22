"""Risk Guard adapter for the multi-agent cycle.

Wraps the independent grow.risk.RiskGuard. Agents cannot modify risk limits.
Consensus cannot override a rejection. 4B consumes this interface only —
final decision integration remains Requirement 4C.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.execution.lock import assert_paper_runtime
from grow.market_data.normalized.models import AgentMarketSnapshot


@dataclass(frozen=True)
class RiskGateDecision:
    approved: bool
    decision: str
    reason: str
    details: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "decision": self.decision,
            "reason": self.reason,
            "details": list(self.details),
            "live_trading": False,
        }


class AgentRiskGuard:
    """Independent safety authority for agent-cycle proposals."""

    agent_name = "risk_guard"
    agent_version = "risk_guard.adapter.v2"

    def __init__(
        self,
        *,
        paper_mode: bool = True,
        live_trading: bool = False,
        execution_mode: str = "paper",
    ) -> None:
        if not paper_mode or live_trading or execution_mode != "paper":
            raise ValueError("AgentRiskGuard requires paper_mode and forbids live trading")
        self.paper_mode = True
        self.live_trading = False
        self.execution_mode = "paper"

    def evaluate(
        self,
        snapshot: AgentMarketSnapshot,
        *,
        proposed_action: CandidateAction,
        proposed_instrument: str | None,
        specialist_results: tuple[AgentResult, ...],
        consensus: bool,
    ) -> RiskGateDecision:
        assert_paper_runtime(self.execution_mode, self.live_trading, "PAPER")
        if snapshot.live_trading or not snapshot.paper_mode:
            return RiskGateDecision(False, "REJECT", "SNAPSHOT_LIVE_TRADING_FORBIDDEN")
        if snapshot.data_quality.value in {"INSUFFICIENT", "REJECTED", "STALE"}:
            return RiskGateDecision(
                False,
                "REJECT",
                f"DATA_QUALITY:{snapshot.data_quality.value}",
                snapshot.quality_notes,
            )
        errors = tuple(row.agent_name for row in specialist_results if row.status is AgentStatus.ERROR)
        if errors:
            return RiskGateDecision(False, "NO_TRADE", "SPECIALIST_ERROR", errors)
        if proposed_action in {CandidateAction.NONE, CandidateAction.ABSTAIN, CandidateAction.HOLD}:
            return RiskGateDecision(False, "NO_TRADE", "NO_CANDIDATE_ACTION")
        if proposed_action is CandidateAction.PAPER_OPEN and not proposed_instrument:
            return RiskGateDecision(False, "REJECT", "MISSING_INSTRUMENT")
        # 4B never auto-approves paper opens from research agents.
        if proposed_action is CandidateAction.PAPER_OPEN:
            reason = "FOUNDATION_NO_AUTO_OPEN"
            if consensus:
                reason = "CONSENSUS_CANNOT_OVERRIDE_FOUNDATION_BLOCK"
            return RiskGateDecision(False, "REJECT", reason)
        return RiskGateDecision(False, "NO_TRADE", f"UNSUPPORTED_ACTION:{proposed_action.value}")

    def analyze(self, snapshot: AgentMarketSnapshot, *, cycle_id: str = "") -> AgentResult:
        """Expose Risk Guard as a structured agent result without execution rights."""
        gate = self.evaluate(
            snapshot,
            proposed_action=CandidateAction.NONE,
            proposed_instrument=None,
            specialist_results=(),
            consensus=False,
        )
        status = AgentStatus.PASS if gate.decision != "REJECT" else AgentStatus.ERROR
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            decision_timestamp=snapshot.decision_timestamp,
            status=status,
            observations=(gate.reason, *gate.details),
            calculated_metrics={"paper_mode": True},
            interpretation=("Risk Guard adapter validation only; not a trade order.",),
            findings=(gate.decision,),
            data_quality_concerns=(),
            assumptions=(),
            evidence=(f"snapshot_id={snapshot.snapshot_id}",),
            metrics_used=("paper_mode", "data_quality"),
            candidate_action=CandidateAction.NONE,
            candidate_instrument=None,
            entry_reason=None,
            invalidation_reason=gate.reason,
            risk_flags=(gate.decision,),
            missing_data=(),
            confidence=None,
            cycle_id=cycle_id,
        )
