"""Options Chain Agent — strike/CE-PE structure from the shared snapshot."""

from __future__ import annotations

from grow.agents.common import insufficient, require_quality
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot, DataQualityStatus


class OptionsChainAgent:
    agent_name = "options"
    agent_version = "options.v1"

    def analyze(self, snapshot: AgentMarketSnapshot) -> AgentResult:
        blocked = require_quality(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
        )
        if blocked is not None:
            return blocked
        if not snapshot.option_contracts:
            return insufficient(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=("option_contracts",),
                observations=("option chain absent from shared snapshot",),
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
        unusable_ids = tuple(
            row.provider_contract_id for row in (*stale, *rejected)
        )

        ce = sum(1 for row in usable if row.option_type == "CE")
        pe = sum(1 for row in usable if row.option_type == "PE")
        with_oi = sum(1 for row in usable if (row.open_interest or 0) > 0)
        with_ltp = sum(1 for row in usable if row.ltp is not None)
        observations = (
            f"contracts={len(snapshot.option_contracts)}",
            f"usable={len(usable)}",
            f"stale={len(stale)}",
            f"rejected_or_insufficient={len(rejected)}",
            f"ce_usable={ce}",
            f"pe_usable={pe}",
            f"with_oi_usable={with_oi}",
            f"with_ltp_usable={with_ltp}",
            "fact: per-option quality retained from shared snapshot",
            "rule: stale options are never trade candidates",
            "conclusion: chain inspected; no trade emitted by foundation agent",
        )
        if unusable_ids:
            observations = observations + tuple(f"unusable:{cid}" for cid in unusable_ids)

        if not usable:
            return insufficient(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=("usable_option_contracts", *unusable_ids),
                observations=observations,
            )

        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.NO_TRADE,
            observations=observations,
            metrics_used=(
                "contract_count",
                "usable_count",
                "stale_count",
                "ce_count",
                "pe_count",
                "oi_count",
            ),
            candidate_action=CandidateAction.NONE,
            candidate_instrument=None,
            entry_reason=None,
            invalidation_reason="options agent does not select strikes in foundation phase",
            risk_flags=("STALE_OPTIONS_EXCLUDED",) if stale else (),
            missing_data=(),
            confidence=None,
        )
