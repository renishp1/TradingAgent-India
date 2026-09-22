"""Shared helpers for deterministic specialist agents."""

from __future__ import annotations

from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot, DataQualityStatus
from grow.market_data.snapshots.builder import gate_snapshot_quality


def insufficient(
    *,
    agent_name: str,
    agent_version: str,
    snapshot: AgentMarketSnapshot,
    missing: tuple[str, ...],
    observations: tuple[str, ...] = (),
) -> AgentResult:
    return AgentResult(
        agent_name=agent_name,
        agent_version=agent_version,
        snapshot_id=snapshot.snapshot_id,
        decision_timestamp=snapshot.decision_timestamp,
        status=AgentStatus.DATA_INSUFFICIENT,
        observations=observations,
        metrics_used=(),
        candidate_action=CandidateAction.NONE,
        candidate_instrument=None,
        entry_reason=None,
        invalidation_reason="missing or stale market data",
        risk_flags=("DATA_QUALITY",),
        missing_data=missing,
        confidence=None,
    )


def require_quality(
    *,
    agent_name: str,
    agent_version: str,
    snapshot: AgentMarketSnapshot,
) -> AgentResult | None:
    gated = gate_snapshot_quality(snapshot)
    if gated in {
        DataQualityStatus.INSUFFICIENT,
        DataQualityStatus.REJECTED,
        DataQualityStatus.STALE,
    }:
        return insufficient(
            agent_name=agent_name,
            agent_version=agent_version,
            snapshot=snapshot,
            missing=(f"data_quality:{gated.value}", *snapshot.quality_notes),
            observations=(f"quality_gate={gated.value}",),
        )
    return None
