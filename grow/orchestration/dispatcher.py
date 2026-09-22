"""Dispatch the same immutable snapshot to eligible specialists."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime

from grow.agents.base import SpecialistAgent
from grow.clock import IST
from grow.decision.contracts.agent_result import AgentInput, AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot
from grow.orchestration.models import AgentDispatchRecord
from grow.orchestration.validator import validate_agent_output


def _now() -> datetime:
    return datetime.now(tz=IST)


def dispatch_agents(
    *,
    cycle_id: str,
    snapshot: AgentMarketSnapshot,
    specialists: tuple[SpecialistAgent, ...] | list[SpecialistAgent],
    timeout_seconds: float = 2.0,
) -> tuple[tuple[AgentResult, ...], tuple[dict, ...], tuple[AgentDispatchRecord, ...]]:
    """Run agents independently; validate each output; preserve failures."""
    accepted: list[AgentResult] = []
    rejected: list[dict] = []
    records: list[AgentDispatchRecord] = []

    for agent in specialists:
        started = _now()
        name = getattr(agent, "agent_name", type(agent).__name__)
        version = getattr(agent, "agent_version", "unknown")
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_call_agent, agent, snapshot, cycle_id)
                raw = future.result(timeout=timeout_seconds)
            outcome = validate_agent_output(raw, snapshot=snapshot, cycle_id=cycle_id)
            ended = _now()
            if not outcome.accepted or outcome.result is None:
                rejected.append(
                    {
                        "agent_name": name,
                        "agent_version": version,
                        "reason": outcome.reason or "VALIDATION_FAILED",
                        "raw": None if not isinstance(raw, AgentResult) else raw.to_dict(),
                    }
                )
                records.append(
                    AgentDispatchRecord(
                        agent_name=name,
                        agent_version=version,
                        status="REJECTED",
                        started_at=started,
                        ended_at=ended,
                        accepted=False,
                        rejection_reason=outcome.reason,
                    )
                )
                continue
            accepted.append(outcome.result)
            records.append(
                AgentDispatchRecord(
                    agent_name=name,
                    agent_version=version,
                    status=outcome.result.status.value,
                    started_at=started,
                    ended_at=ended,
                    accepted=True,
                    execution_time_ms=outcome.result.execution_time_ms,
                )
            )
        except FuturesTimeout:
            ended = _now()
            error = _error_result(name, version, snapshot, cycle_id, reason="TIMEOUT")
            accepted.append(error)
            records.append(
                AgentDispatchRecord(
                    agent_name=name,
                    agent_version=version,
                    status=AgentStatus.ERROR.value,
                    started_at=started,
                    ended_at=ended,
                    accepted=True,
                    rejection_reason="TIMEOUT",
                )
            )
        except Exception as exc:  # one failed specialist must not crash the cycle
            ended = _now()
            error = _error_result(
                name,
                version,
                snapshot,
                cycle_id,
                reason=f"{type(exc).__name__}:{str(exc)[:160]}",
            )
            accepted.append(error)
            records.append(
                AgentDispatchRecord(
                    agent_name=name,
                    agent_version=version,
                    status=AgentStatus.ERROR.value,
                    started_at=started,
                    ended_at=ended,
                    accepted=True,
                    rejection_reason=type(exc).__name__,
                )
            )

    return tuple(accepted), tuple(rejected), tuple(records)


def _call_agent(agent: SpecialistAgent, snapshot: AgentMarketSnapshot, cycle_id: str) -> AgentResult:
    agent_input = AgentInput(
        cycle_id=cycle_id,
        snapshot=snapshot,
        agent_config_version=getattr(agent, "agent_version", ""),
    )
    analyze_input = getattr(agent, "analyze_input", None)
    if callable(analyze_input):
        return analyze_input(agent_input)
    return agent.analyze(snapshot, cycle_id=cycle_id)


def _error_result(
    name: str,
    version: str,
    snapshot: AgentMarketSnapshot,
    cycle_id: str,
    *,
    reason: str,
) -> AgentResult:
    return AgentResult(
        agent_name=name,
        agent_version=version,
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        decision_timestamp=snapshot.decision_timestamp,
        status=AgentStatus.ERROR,
        observations=(f"agent_failure:{reason}",),
        calculated_metrics={},
        interpretation=("Agent unavailable; no fabricated substitute output.",),
        findings=("AGENT_UNAVAILABLE",),
        data_quality_concerns=(),
        assumptions=(),
        evidence=(f"snapshot_id={snapshot.snapshot_id}", f"cycle_id={cycle_id}"),
        metrics_used=(),
        candidate_action=CandidateAction.NONE,
        candidate_instrument=None,
        entry_reason=None,
        invalidation_reason=reason,
        risk_flags=("AGENT_FAILURE",),
        missing_data=(),
        confidence=None,
        cycle_id=cycle_id,
    )
