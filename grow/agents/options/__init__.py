"""Options Chain Agent — strike/CE-PE structure from the shared snapshot."""

from __future__ import annotations

from grow.agents.common import insufficient, require_quality
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot


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
        ce = sum(1 for row in snapshot.option_contracts if row.option_type == "CE")
        pe = sum(1 for row in snapshot.option_contracts if row.option_type == "PE")
        with_oi = sum(1 for row in snapshot.option_contracts if (row.open_interest or 0) > 0)
        with_ltp = sum(1 for row in snapshot.option_contracts if row.ltp is not None)
        observations = (
            f"contracts={len(snapshot.option_contracts)}",
            f"ce={ce}",
            f"pe={pe}",
            f"with_oi={with_oi}",
            f"with_ltp={with_ltp}",
            "fact: counts derived from shared snapshot only",
            "conclusion: chain present; no trade emitted by foundation agent",
        )
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.NO_TRADE,
            observations=observations,
            metrics_used=("contract_count", "ce_count", "pe_count", "oi_count"),
            candidate_action=CandidateAction.NONE,
            candidate_instrument=None,
            entry_reason=None,
            invalidation_reason="options agent does not select strikes in foundation phase",
            risk_flags=(),
            missing_data=(),
            confidence=None,
        )
