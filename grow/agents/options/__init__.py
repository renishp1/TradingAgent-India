"""Options Chain Agent — CE/PE structure from the shared snapshot only."""

from __future__ import annotations

from grow.agents.common import make_result, no_data, require_quality, timed_ms
from grow.decision.contracts.agent_result import AgentInput, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot, DataQualityStatus


class OptionsChainAgent:
    agent_name = "options"
    agent_version = "options.v2"

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
        if not snapshot.option_contracts:
            return no_data(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=("option_contracts",),
                observations=("option chain absent from shared snapshot",),
                cycle_id=cycle_id,
            )

        usable = tuple(
            row for row in snapshot.option_contracts if row.quality is DataQualityStatus.OK
        )
        stale = tuple(
            row for row in snapshot.option_contracts if row.quality is DataQualityStatus.STALE
        )
        rejected = tuple(
            row
            for row in snapshot.option_contracts
            if row.quality in {DataQualityStatus.REJECTED, DataQualityStatus.INSUFFICIENT}
        )
        unusable_ids = tuple(row.provider_contract_id for row in (*stale, *rejected))

        if not usable:
            return no_data(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=("usable_option_contracts", *unusable_ids),
                observations=(
                    f"contracts={len(snapshot.option_contracts)}",
                    f"usable=0",
                    f"stale={len(stale)}",
                    f"rejected_or_insufficient={len(rejected)}",
                    "rule: stale options are never trade candidates",
                ),
                cycle_id=cycle_id,
                data_quality_concerns=tuple(f"unusable:{cid}" for cid in unusable_ids),
            )

        ce = sum(1 for row in usable if row.option_type == "CE")
        pe = sum(1 for row in usable if row.option_type == "PE")
        with_oi = sum(1 for row in usable if (row.open_interest or 0) > 0)
        with_ltp = sum(1 for row in usable if row.ltp is not None)
        missing_bid_ask = [
            row.provider_contract_id for row in usable if row.bid is None or row.ask is None
        ]
        missing_ltp = [row.provider_contract_id for row in usable if row.ltp is None]
        expiries = sorted({row.expiry.isoformat() for row in usable})
        strikes = sorted({row.strike for row in usable})

        concerns = tuple(
            [
                *(f"missing_bid_ask:{cid}" for cid in missing_bid_ask),
                *(f"missing_ltp:{cid}" for cid in missing_ltp),
                *(f"stale:{cid}" for cid in (row.provider_contract_id for row in stale)),
                *(f"rejected:{cid}" for cid in (row.provider_contract_id for row in rejected)),
            ]
        )
        status = AgentStatus.DEGRADED if concerns else AgentStatus.PASS
        metrics = {
            "contract_count": len(snapshot.option_contracts),
            "usable_count": len(usable),
            "stale_count": len(stale),
            "rejected_count": len(rejected),
            "ce_count": ce,
            "pe_count": pe,
            "with_oi": with_oi,
            "with_ltp": with_ltp,
            "expiry_count": len(expiries),
            "strike_count": len(strikes),
            "missing_bid_ask_count": len(missing_bid_ask),
            "missing_ltp_count": len(missing_ltp),
        }
        return make_result(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
            status=status,
            observations=(
                f"contracts={len(snapshot.option_contracts)}",
                f"usable={len(usable)}",
                f"stale={len(stale)}",
                f"rejected_or_insufficient={len(rejected)}",
                f"ce_usable={ce}",
                f"pe_usable={pe}",
                f"expiries={','.join(expiries)}",
            ),
            calculated_metrics=metrics,
            interpretation=(
                "Chain structure summarized from supplied contracts only; no contracts invented.",
                "Stale options are excluded from usable counts and are never trade candidates.",
            ),
            findings=(
                "CHAIN_PRESENT",
                *(("PARTIAL_QUOTE_COVERAGE",) if concerns else ()),
                *(("STALE_OPTIONS_EXCLUDED",) if stale else ()),
            ),
            data_quality_concerns=concerns,
            assumptions=("Only contracts present in the normalized snapshot are considered.",),
            evidence=(
                f"snapshot_id={snapshot.snapshot_id}",
                f"version={snapshot.version}",
                f"usable_ids={','.join(row.provider_contract_id for row in usable)}",
            ),
            metrics_used=(
                "contract_count",
                "usable_count",
                "stale_count",
                "ce_count",
                "pe_count",
                "oi_count",
                "ltp_count",
            ),
            candidate_action=CandidateAction.NONE,
            invalidation_reason="options agent does not select strikes for execution",
            risk_flags=("STALE_OPTIONS_EXCLUDED",) if stale else (),
            cycle_id=cycle_id,
            confidence=0.7 if status is AgentStatus.PASS else 0.4,
        )
