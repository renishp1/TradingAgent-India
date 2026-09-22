"""Strategy Research Agent — evaluates configured strategies; invents no history."""

from __future__ import annotations

from grow.agents.common import require_quality
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot


class StrategyResearchAgent:
    """Foundation agent: reports configured strategy names only.

    It must not invent historical performance or claim profitability.
    Full strategy evaluation against MarketSnapshot remains in grow.strategies.
    """

    agent_name = "strategy_research"
    agent_version = "strategy_research.v1"

    def __init__(self, configured_strategies: tuple[str, ...] = ()) -> None:
        self.configured_strategies = configured_strategies

    def analyze(self, snapshot: AgentMarketSnapshot) -> AgentResult:
        blocked = require_quality(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
        )
        if blocked is not None:
            return blocked
        if not self.configured_strategies:
            return AgentResult(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot_id=snapshot.snapshot_id,
                decision_timestamp=snapshot.decision_timestamp,
                status=AgentStatus.NO_TRADE,
                observations=("no strategies configured for evaluation",),
                metrics_used=(),
                candidate_action=CandidateAction.NONE,
                candidate_instrument=None,
                entry_reason=None,
                invalidation_reason="strategy list empty",
                risk_flags=(),
                missing_data=("configured_strategies",),
                confidence=None,
            )
        observations = (
            f"configured={','.join(self.configured_strategies)}",
            "fact: strategy names come from configuration only",
            "assumption: historical performance is unavailable in this foundation pass",
            "conclusion: NO_TRADE until out-of-sample evaluation exists",
        )
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.NO_TRADE,
            observations=observations,
            metrics_used=("configured_strategy_count",),
            candidate_action=CandidateAction.NONE,
            candidate_instrument=None,
            entry_reason=None,
            invalidation_reason="no out-of-sample evidence attached",
            risk_flags=(),
            missing_data=("out_of_sample_evidence",),
            confidence=None,
        )
