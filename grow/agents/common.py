"""Shared helpers for deterministic specialist agents."""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot, DataQualityStatus
from grow.market_data.snapshots.builder import gate_snapshot_quality


def timed_ms(fn: Callable[[], AgentResult]) -> AgentResult:
    started = time.perf_counter()
    result = fn()
    elapsed = (time.perf_counter() - started) * 1000.0
    if result.execution_time_ms is not None:
        return result
    return AgentResult(
        agent_name=result.agent_name,
        agent_version=result.agent_version,
        snapshot_id=result.snapshot_id,
        snapshot_version=result.snapshot_version,
        decision_timestamp=result.decision_timestamp,
        status=result.status,
        observations=result.observations,
        calculated_metrics=dict(result.calculated_metrics),
        interpretation=result.interpretation,
        findings=result.findings,
        data_quality_concerns=result.data_quality_concerns,
        assumptions=result.assumptions,
        evidence=result.evidence,
        metrics_used=result.metrics_used,
        candidate_action=result.candidate_action,
        candidate_instrument=result.candidate_instrument,
        entry_reason=result.entry_reason,
        invalidation_reason=result.invalidation_reason,
        risk_flags=result.risk_flags,
        missing_data=result.missing_data,
        confidence=result.confidence,
        cycle_id=result.cycle_id,
        execution_time_ms=round(elapsed, 3),
        schema_version=result.schema_version,
    )


def make_result(
    *,
    agent_name: str,
    agent_version: str,
    snapshot: AgentMarketSnapshot,
    status: AgentStatus,
    observations: tuple[str, ...] = (),
    calculated_metrics: Mapping[str, Any] | None = None,
    interpretation: tuple[str, ...] = (),
    findings: tuple[str, ...] = (),
    data_quality_concerns: tuple[str, ...] = (),
    assumptions: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
    metrics_used: tuple[str, ...] = (),
    candidate_action: CandidateAction = CandidateAction.NONE,
    candidate_instrument: str | None = None,
    entry_reason: str | None = None,
    invalidation_reason: str | None = None,
    risk_flags: tuple[str, ...] = (),
    missing_data: tuple[str, ...] = (),
    confidence: float | None = None,
    cycle_id: str = "",
    execution_time_ms: float | None = None,
) -> AgentResult:
    return AgentResult(
        agent_name=agent_name,
        agent_version=agent_version,
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        decision_timestamp=snapshot.decision_timestamp,
        status=status,
        observations=observations,
        calculated_metrics=dict(calculated_metrics or {}),
        interpretation=interpretation,
        findings=findings,
        data_quality_concerns=data_quality_concerns,
        assumptions=assumptions,
        evidence=evidence,
        metrics_used=metrics_used,
        candidate_action=candidate_action,
        candidate_instrument=candidate_instrument,
        entry_reason=entry_reason,
        invalidation_reason=invalidation_reason,
        risk_flags=risk_flags,
        missing_data=missing_data,
        confidence=confidence,
        cycle_id=cycle_id,
        execution_time_ms=execution_time_ms,
    )


def no_data(
    *,
    agent_name: str,
    agent_version: str,
    snapshot: AgentMarketSnapshot,
    missing: tuple[str, ...],
    observations: tuple[str, ...] = (),
    cycle_id: str = "",
    data_quality_concerns: tuple[str, ...] = (),
) -> AgentResult:
    return make_result(
        agent_name=agent_name,
        agent_version=agent_version,
        snapshot=snapshot,
        status=AgentStatus.NO_DATA,
        observations=observations,
        missing_data=missing,
        data_quality_concerns=data_quality_concerns or missing,
        invalidation_reason="missing or stale market data",
        risk_flags=("DATA_QUALITY",),
        cycle_id=cycle_id,
        evidence=(f"snapshot_id={snapshot.snapshot_id}", f"version={snapshot.version}"),
    )


# Backward-compatible alias used by earlier foundation helpers.
insufficient = no_data


def require_quality(
    *,
    agent_name: str,
    agent_version: str,
    snapshot: AgentMarketSnapshot,
    cycle_id: str = "",
) -> AgentResult | None:
    gated = gate_snapshot_quality(snapshot)
    if gated in {
        DataQualityStatus.INSUFFICIENT,
        DataQualityStatus.REJECTED,
        DataQualityStatus.STALE,
    }:
        return no_data(
            agent_name=agent_name,
            agent_version=agent_version,
            snapshot=snapshot,
            missing=(f"data_quality:{gated.value}", *snapshot.quality_notes),
            observations=(f"quality_gate={gated.value}",),
            cycle_id=cycle_id,
            data_quality_concerns=(f"quality_gate={gated.value}", *snapshot.quality_notes),
        )
    return None
