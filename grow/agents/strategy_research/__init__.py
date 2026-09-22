"""Strategy Research Agent — buyer-only research candidates; no execution."""

from __future__ import annotations

from grow.agents.common import make_result, require_quality, timed_ms
from grow.decision.contracts.agent_result import AgentInput, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot


class StrategyResearchAgent:
    """Maps observations to configured buyer-only strategy candidates.

    Does not place trades, invent performance, or bypass risk controls.
    """

    agent_name = "strategy_research"
    agent_version = "strategy_research.v2"

    def __init__(self, configured_strategies: tuple[str, ...] = ()) -> None:
        self.configured_strategies = configured_strategies

    def analyze(self, snapshot: AgentMarketSnapshot, *, cycle_id: str = ""):
        return timed_ms(lambda: self._analyze(snapshot, cycle_id=cycle_id))

    def analyze_input(self, agent_input: AgentInput):
        return self.analyze(agent_input.snapshot, cycle_id=agent_input.cycle_id)

    def _analyze(self, snapshot: AgentMarketSnapshot, *, cycle_id: str = ""):
        blocked = require_quality(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
            cycle_id=cycle_id,
        )
        if blocked is not None:
            return blocked
        if not self.configured_strategies:
            return make_result(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                status=AgentStatus.NO_DATA,
                observations=("no strategies configured for evaluation",),
                findings=("NO_STRATEGIES_CONFIGURED",),
                missing_data=("configured_strategies",),
                data_quality_concerns=("configured_strategies_empty",),
                invalidation_reason="strategy list empty",
                cycle_id=cycle_id,
                evidence=(f"snapshot_id={snapshot.snapshot_id}",),
            )

        candidates = tuple(self.configured_strategies)
        return make_result(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
            status=AgentStatus.PASS,
            observations=(
                f"configured={','.join(candidates)}",
                f"underlying_count={len(snapshot.underlyings)}",
                f"option_count={len(snapshot.option_contracts)}",
            ),
            calculated_metrics={
                "configured_strategy_count": len(candidates),
                "candidates": list(candidates),
            },
            interpretation=(
                "Configured buyer-only strategy names mapped as research candidates only.",
            ),
            findings=("RESEARCH_CANDIDATES_LISTED", "NO_PROFITABILITY_CLAIM"),
            assumptions=(
                "Historical / out-of-sample performance is unavailable in this cycle.",
                "Buyer-only constraint remains in force.",
            ),
            evidence=(
                f"snapshot_id={snapshot.snapshot_id}",
                f"version={snapshot.version}",
                f"strategies={','.join(candidates)}",
            ),
            metrics_used=("configured_strategy_count",),
            candidate_action=CandidateAction.NONE,
            invalidation_reason="no out-of-sample evidence attached",
            missing_data=("out_of_sample_evidence",),
            data_quality_concerns=("out_of_sample_evidence_absent",),
            cycle_id=cycle_id,
            confidence=0.2,
        )
