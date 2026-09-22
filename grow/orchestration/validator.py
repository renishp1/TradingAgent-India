"""Validate specialist outputs against the 4B common contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from grow.decision.contracts.agent_result import AGENT_RESULT_SCHEMA, AgentResult, AgentStatus
from grow.market_data.normalized.models import AgentMarketSnapshot


@dataclass(frozen=True)
class ValidationOutcome:
    accepted: bool
    reason: str | None = None
    result: AgentResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "result": None if self.result is None else self.result.to_dict(),
        }


REQUIRED_FIELDS = (
    "agent_name",
    "agent_version",
    "snapshot_id",
    "snapshot_version",
    "status",
    "observations",
    "calculated_metrics",
    "interpretation",
    "findings",
    "data_quality_concerns",
    "assumptions",
    "evidence",
)


def validate_agent_output(
    result: AgentResult,
    *,
    snapshot: AgentMarketSnapshot,
    cycle_id: str,
) -> ValidationOutcome:
    """Reject mismatched snapshot identity or malformed outputs."""
    if not isinstance(result, AgentResult):
        return ValidationOutcome(False, "NOT_AGENT_RESULT")
    if result.schema_version != AGENT_RESULT_SCHEMA:
        return ValidationOutcome(False, "SCHEMA_MISMATCH")
    if result.snapshot_id != snapshot.snapshot_id:
        return ValidationOutcome(False, "SNAPSHOT_ID_MISMATCH")
    if result.snapshot_version != snapshot.version:
        return ValidationOutcome(False, "SNAPSHOT_VERSION_MISMATCH")
    if result.cycle_id and result.cycle_id != cycle_id:
        return ValidationOutcome(False, "CYCLE_ID_MISMATCH")
    if result.status not in set(AgentStatus):
        return ValidationOutcome(False, "INVALID_STATUS")
    if result.status is AgentStatus.NO_DATA and not result.missing_data:
        return ValidationOutcome(False, "NO_DATA_MISSING_FIELDS")
    payload = result.to_dict()
    for field in REQUIRED_FIELDS:
        if field not in payload:
            return ValidationOutcome(False, f"MISSING_FIELD:{field}")
    if payload.get("live_trading") is True or payload.get("executed") is True:
        return ValidationOutcome(False, "EXECUTION_FLAG_FORBIDDEN")
    # Stamp cycle_id if the agent left it blank.
    if not result.cycle_id:
        stamped = AgentResult(
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
            cycle_id=cycle_id,
            execution_time_ms=result.execution_time_ms,
            schema_version=result.schema_version,
        )
        return ValidationOutcome(True, None, stamped)
    return ValidationOutcome(True, None, result)
