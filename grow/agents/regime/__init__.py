"""Market Regime Agent — reproducible environment labels with evidence."""

from __future__ import annotations

from grow.agents.common import make_result, no_data, require_quality, timed_ms
from grow.decision.contracts.agent_result import AgentInput, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot


class RegimeAgent:
    agent_name = "regime"
    agent_version = "regime.v2"

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
        if not snapshot.underlyings:
            return no_data(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=("underlyings",),
                cycle_id=cycle_id,
            )
        quote = next(iter(snapshot.underlyings.values()))
        if quote.ltp is None or quote.high is None or quote.low is None or quote.ltp <= 0:
            return no_data(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=("ltp", "high", "low"),
                cycle_id=cycle_id,
            )

        range_pct = (quote.high - quote.low) / quote.ltp
        if quote.open is None or quote.close is None:
            label = "UNKNOWN"
            status = AgentStatus.DEGRADED
            confidence = 0.2
            findings = ("INSUFFICIENT_DATA", "UNKNOWN")
            interpretation = (
                "Open/close unavailable; regime cannot be classified beyond range volatility.",
            )
        elif range_pct >= 0.02:
            label = "HIGH_VOLATILITY"
            status = AgentStatus.PASS
            confidence = 0.6
            findings = (label,)
            interpretation = ("Wide single-bar range relative to LTP suggests elevated volatility.",)
        elif quote.close > quote.open:
            label = "TRENDING_UP_CANDIDATE"
            status = AgentStatus.PASS
            confidence = 0.45
            findings = (label,)
            interpretation = ("Close above open with modest range — provisional uptrend candidate.",)
        elif quote.close < quote.open:
            label = "TRENDING_DOWN_CANDIDATE"
            status = AgentStatus.PASS
            confidence = 0.45
            findings = (label,)
            interpretation = (
                "Close below open with modest range — provisional downtrend candidate.",
            )
        else:
            label = "RANGING_CANDIDATE"
            status = AgentStatus.PASS
            confidence = 0.4
            findings = (label,)
            interpretation = ("Flat open/close with modest range — provisional ranging label.",)

        return make_result(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
            status=status,
            observations=(
                f"underlying={quote.underlying}",
                f"ltp={quote.ltp}",
                f"range_pct={range_pct:.4f}",
                f"open={quote.open}",
                f"close={quote.close}",
            ),
            calculated_metrics={
                "range_pct": round(range_pct, 6),
                "regime_label": label,
            },
            interpretation=interpretation,
            findings=findings,
            assumptions=(
                "Single-bar features only; not a future-direction claim.",
                "Labels are candidates, not certainties.",
            ),
            evidence=(
                f"snapshot_id={snapshot.snapshot_id}",
                f"version={snapshot.version}",
                f"range_pct={range_pct:.6f}",
            ),
            metrics_used=("range_pct", "open", "close"),
            candidate_action=CandidateAction.ABSTAIN,
            candidate_instrument=quote.underlying,
            entry_reason=f"regime={label}",
            invalidation_reason="regime labels are provisional and not trade orders",
            cycle_id=cycle_id,
            confidence=confidence,
        )
