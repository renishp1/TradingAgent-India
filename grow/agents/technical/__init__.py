"""Technical Analysis Agent — price structure from normalized data only."""

from __future__ import annotations

from grow.agents.common import insufficient, require_quality
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot, UnderlyingQuoteView


class TechnicalAgent:
    agent_name = "technical"
    agent_version = "technical.v1"

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
        first = next(iter(snapshot.underlyings.values()))
        missing = _missing_ohlc(first)
        if missing:
            return insufficient(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=missing,
            )
        range_pct = 0.0
        if first.ltp and first.high is not None and first.low is not None and first.ltp > 0:
            range_pct = (first.high - first.low) / first.ltp
        observations = (
            f"underlying={first.underlying}",
            f"ltp={first.ltp}",
            f"range_pct={range_pct:.4f}",
            "fact: OHLC taken from shared snapshot",
            "assumption: single-bar range is a weak structure proxy only",
        )
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.NO_TRADE,
            observations=observations,
            metrics_used=("ltp", "high", "low", "range_pct"),
            candidate_action=CandidateAction.NONE,
            candidate_instrument=first.underlying,
            entry_reason=None,
            invalidation_reason="technical agent is advisory; does not emit entries in foundation",
            risk_flags=(),
            missing_data=(),
            confidence=None,
        )


def _missing_ohlc(quote: UnderlyingQuoteView) -> tuple[str, ...]:
    missing: list[str] = []
    if quote.ltp is None and quote.spot is None:
        missing.append("ltp")
    if quote.high is None:
        missing.append("high")
    if quote.low is None:
        missing.append("low")
    return tuple(missing)
