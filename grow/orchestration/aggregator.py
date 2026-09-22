"""Conflict-preserving aggregation of specialist outputs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from grow.decision.aggregation.debate import DebateSummary, summarize_debate
from grow.decision.contracts.agent_result import AgentResult, AgentStatus
from grow.orchestration.models import AgentDispatchRecord, AggregateAnalysisPackage


def aggregate_outputs(
    *,
    cycle_id: str,
    snapshot_id: str,
    snapshot_version: str,
    as_of,
    outputs: tuple[AgentResult, ...],
    rejected_outputs: tuple[dict[str, Any], ...],
    dispatch_records: tuple[AgentDispatchRecord, ...],
) -> AggregateAnalysisPackage:
    debate = summarize_debate(outputs)
    conflicts = list(debate.conflicts)

    # Finding-level conflicts (e.g. bullish technical vs bearish regime).
    bullish = []
    bearish = []
    for row in outputs:
        blob = " ".join(row.findings + row.interpretation).upper()
        if any(token in blob for token in ("BULL", "TRENDING_UP", "SMA_FAST_ABOVE")):
            bullish.append(row.agent_name)
        if any(token in blob for token in ("BEAR", "TRENDING_DOWN", "SMA_FAST_BELOW")):
            bearish.append(row.agent_name)
    if bullish and bearish:
        conflicts.append(f"DIRECTION_CONFLICT:bullish={','.join(bullish)};bearish={','.join(bearish)}")

    unavailable = tuple(
        row.agent_name
        for row in outputs
        if row.status is AgentStatus.ERROR
    ) + tuple(item.get("agent_name", "unknown") for item in rejected_outputs)

    supporting = tuple(
        f"{row.agent_name}:{finding}"
        for row in outputs
        if row.status in {AgentStatus.PASS, AgentStatus.DEGRADED}
        for finding in (row.findings or row.observations[:1])
    )
    conflicting = tuple(debate.evidence) + tuple(c for c in conflicts if c.startswith("DIRECTION_"))

    summary = _deterministic_summary(outputs, tuple(conflicts), unavailable)
    digest = _package_digest(
        {
            "cycle_id": cycle_id,
            "snapshot_id": snapshot_id,
            "snapshot_version": snapshot_version,
            "outputs": [row.to_dict() for row in outputs],
            "rejected": list(rejected_outputs),
            "conflicts": conflicts,
            "summary": summary,
        }
    )
    return AggregateAnalysisPackage(
        cycle_id=cycle_id,
        snapshot_id=snapshot_id,
        snapshot_version=snapshot_version,
        as_of=as_of,
        agent_outputs=outputs,
        rejected_outputs=rejected_outputs,
        dispatch_records=dispatch_records,
        conflicts=tuple(dict.fromkeys(conflicts)),
        supporting_evidence=supporting,
        conflicting_evidence=tuple(dict.fromkeys(conflicting)),
        unavailable_agents=tuple(dict.fromkeys(unavailable)),
        debate=debate,
        cycle_summary=summary,
        package_digest=digest,
    )


def _deterministic_summary(
    outputs: tuple[AgentResult, ...],
    conflicts: tuple[str, ...],
    unavailable: tuple[str, ...],
) -> str:
    parts = [
        f"agents={len(outputs)}",
        f"pass={sum(1 for r in outputs if r.status is AgentStatus.PASS)}",
        f"degraded={sum(1 for r in outputs if r.status is AgentStatus.DEGRADED)}",
        f"no_data={sum(1 for r in outputs if r.status is AgentStatus.NO_DATA)}",
        f"error={sum(1 for r in outputs if r.status is AgentStatus.ERROR)}",
        f"conflicts={len(conflicts)}",
        f"unavailable={','.join(unavailable) if unavailable else 'none'}",
    ]
    return "|".join(parts)


def _package_digest(payload: dict[str, Any]) -> str:
    cleaned = _strip_timing(payload)
    body = json.dumps(cleaned, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _strip_timing(value: Any) -> Any:
    """Exclude wall-clock fields so digests stay stable across runs."""
    if isinstance(value, dict):
        return {
            key: _strip_timing(item)
            for key, item in value.items()
            if key
            not in {
                "execution_time_ms",
                "started_at",
                "ended_at",
            }
        }
    if isinstance(value, list):
        return [_strip_timing(item) for item in value]
    return value
