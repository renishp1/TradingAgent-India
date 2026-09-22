"""Market Data Analyst — validates snapshot quality and session context."""

from __future__ import annotations

from grow.agents.common import insufficient, require_quality
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot


class MarketDataAgent:
    agent_name = "market_data"
    agent_version = "market_data.v1"

    def analyze(self, snapshot: AgentMarketSnapshot) -> AgentResult:
        blocked = require_quality(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
        )
        if blocked is not None:
            return blocked
        if not snapshot.underlyings:
            return insufficient(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=("underlyings",),
            )
        observations = [
            f"provider={snapshot.provider}",
            f"quality={snapshot.data_quality.value}",
            f"underlyings={len(snapshot.underlyings)}",
            f"options={len(snapshot.option_contracts)}",
        ]
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=tuple(observations),
            metrics_used=("data_quality", "underlying_count", "option_count"),
            candidate_action=CandidateAction.ABSTAIN,
            candidate_instrument=None,
            entry_reason="market data validated for agent cycle",
            invalidation_reason="stale or incomplete snapshot",
            risk_flags=(),
            missing_data=(),
            confidence=None,
        )
