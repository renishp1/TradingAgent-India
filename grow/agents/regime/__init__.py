"""Market Regime Agent — deterministic, explainable environment labels."""

from __future__ import annotations

from grow.agents.common import insufficient, require_quality
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot


class RegimeAgent:
    agent_name = "regime"
    agent_version = "regime.v1"

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
        quote = next(iter(snapshot.underlyings.values()))
        if quote.ltp is None or quote.high is None or quote.low is None or quote.ltp <= 0:
            return insufficient(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=("ltp", "high", "low"),
            )
        range_pct = (quote.high - quote.low) / quote.ltp
        if range_pct >= 0.02:
            label = "HIGH_VOLATILITY"
        elif quote.close is not None and quote.open is not None and quote.close > quote.open:
            label = "TRENDING_UP_CANDIDATE"
        elif quote.close is not None and quote.open is not None and quote.close < quote.open:
            label = "TRENDING_DOWN_CANDIDATE"
        else:
            label = "RANGING_CANDIDATE"
        observations = (
            f"regime_label={label}",
            f"range_pct={range_pct:.4f}",
            "assumption: single-bar features only; not a certainty claim",
            "conclusion: regime classification is provisional",
        )
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=observations,
            metrics_used=("range_pct", "open", "close"),
            candidate_action=CandidateAction.ABSTAIN,
            candidate_instrument=quote.underlying,
            entry_reason=f"regime={label}",
            invalidation_reason="regime labels are provisional and not trade orders",
            risk_flags=(),
            missing_data=(),
            confidence=None,
        )
