"""Market Data Analyst — validates snapshot quality; never fabricates data."""

from __future__ import annotations

from grow.agents.common import make_result, no_data, require_quality, timed_ms
from grow.decision.contracts.agent_result import AgentInput, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot
from grow.market_data.snapshots.builder import gate_snapshot_quality


class MarketDataAgent:
    agent_name = "market_data"
    agent_version = "market_data.v2"

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

        concerns: list[str] = []
        if snapshot.data_quality.value == "DEGRADED":
            concerns.extend(snapshot.quality_notes)
        missing_fields: list[str] = []
        for symbol, quote in snapshot.underlyings.items():
            if quote.ltp is None and quote.spot is None:
                missing_fields.append(f"{symbol}:ltp")
            if quote.quote_timestamp is None:
                concerns.append(f"{symbol}:missing_quote_timestamp")
            if quote.quote_age_seconds is not None and quote.quote_age_seconds > 60:
                concerns.append(f"{symbol}:stale_age={quote.quote_age_seconds}")

        for opt in snapshot.option_contracts:
            if opt.ltp is None:
                concerns.append(f"option_missing_ltp:{opt.provider_contract_id}")
            if opt.bid is None or opt.ask is None:
                concerns.append(f"option_missing_bid_ask:{opt.provider_contract_id}")
            if opt.quality.value in {"STALE", "REJECTED", "INSUFFICIENT"}:
                concerns.append(f"option_quality:{opt.provider_contract_id}:{opt.quality.value}")

        gated = gate_snapshot_quality(snapshot)
        status = AgentStatus.PASS
        if concerns or gated.value == "DEGRADED":
            status = AgentStatus.DEGRADED
        if missing_fields:
            return no_data(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=tuple(missing_fields),
                observations=(
                    f"provider={snapshot.provider}",
                    f"quality={snapshot.data_quality.value}",
                ),
                cycle_id=cycle_id,
                data_quality_concerns=tuple(concerns),
            )

        observations = (
            f"provider={snapshot.provider}",
            f"quality={snapshot.data_quality.value}",
            f"underlyings={len(snapshot.underlyings)}",
            f"options={len(snapshot.option_contracts)}",
            f"session_timestamp={snapshot.session_timestamp.isoformat()}",
            f"decision_timestamp={snapshot.decision_timestamp.isoformat()}",
        )
        return make_result(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
            status=status,
            observations=observations,
            calculated_metrics={
                "underlying_count": len(snapshot.underlyings),
                "option_count": len(snapshot.option_contracts),
                "quality": snapshot.data_quality.value,
            },
            interpretation=(
                "Data-quality status derived from snapshot metadata only; no values fabricated.",
            ),
            findings=(f"data_quality_status={status.value}",),
            data_quality_concerns=tuple(dict.fromkeys(concerns)),
            assumptions=("Freshness thresholds follow the 3C market-data contract.",),
            evidence=(
                f"snapshot_id={snapshot.snapshot_id}",
                f"snapshot_version={snapshot.version}",
                f"provider={snapshot.provider}",
            ),
            metrics_used=("data_quality", "underlying_count", "option_count"),
            candidate_action=CandidateAction.ABSTAIN,
            entry_reason="market data validated for agent cycle",
            invalidation_reason="stale or incomplete snapshot",
            cycle_id=cycle_id,
            confidence=1.0 if status is AgentStatus.PASS else 0.6,
        )
