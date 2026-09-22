"""Dispatch the same immutable snapshot to eligible specialists concurrently.

Orchestration wait is bounded: after ``timeout_seconds`` the caller continues
with fail-closed ERROR results for unfinished specialists. Python threads cannot
safely kill arbitrary running code — timed-out workers may still finish in the
background on the shared pool; they are not claimed to be forcibly terminated.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import datetime

from grow.agents.base import SpecialistAgent
from grow.clock import IST
from grow.decision.contracts.agent_result import AgentInput, AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot
from grow.orchestration.models import AgentDispatchRecord
from grow.orchestration.validator import validate_agent_output

# Shared pool so dispatch never blocks cycle completion on executor context-manager
# shutdown(wait=True). Timed-out work may linger; orchestration does not wait for it.
_POOL_LOCK = threading.Lock()
_POOL: ThreadPoolExecutor | None = None
_POOL_WORKERS = 0
_MIN_POOL_WORKERS = 8


def _shared_pool(needed: int) -> ThreadPoolExecutor:
    global _POOL, _POOL_WORKERS
    with _POOL_LOCK:
        size = max(needed, _MIN_POOL_WORKERS)
        if _POOL is None or _POOL_WORKERS < size:
            if _POOL is not None:
                _POOL.shutdown(wait=False, cancel_futures=True)
            _POOL_WORKERS = size
            _POOL = ThreadPoolExecutor(
                max_workers=_POOL_WORKERS,
                thread_name_prefix="grow-dispatch",
            )
        return _POOL


def dispatch_agents(
    *,
    cycle_id: str,
    snapshot: AgentMarketSnapshot,
    specialists: tuple[SpecialistAgent, ...] | list[SpecialistAgent],
    timeout_seconds: float = 2.0,
) -> tuple[tuple[AgentResult, ...], tuple[dict, ...], tuple[AgentDispatchRecord, ...]]:
    """Run agents concurrently; validate each output; preserve registration order.

    All specialists start together against the same immutable snapshot. Results
    are returned in the input specialist order, not completion order.
    """
    agents = list(specialists)
    if not agents:
        return (), (), ()

    accepted: list[AgentResult | None] = [None] * len(agents)
    rejected: list[dict | None] = [None] * len(agents)
    records: list[AgentDispatchRecord | None] = [None] * len(agents)
    started = _now()

    pool = _shared_pool(len(agents))
    future_to_index: dict[Future[AgentResult], int] = {}
    for index, agent in enumerate(agents):
        future_to_index[pool.submit(_call_agent, agent, snapshot, cycle_id)] = index

    done, pending = wait(future_to_index.keys(), timeout=timeout_seconds)

    for future in done:
        index = future_to_index[future]
        agent = agents[index]
        name = getattr(agent, "agent_name", type(agent).__name__)
        version = getattr(agent, "agent_version", "unknown")
        ended = _now()
        exc = future.exception()
        if exc is not None:
            error = _error_result(
                name,
                version,
                snapshot,
                cycle_id,
                reason=f"{type(exc).__name__}:{str(exc)[:160]}",
            )
            accepted[index] = error
            records[index] = AgentDispatchRecord(
                agent_name=name,
                agent_version=version,
                status=AgentStatus.ERROR.value,
                started_at=started,
                ended_at=ended,
                accepted=True,
                rejection_reason=type(exc).__name__,
            )
            continue
        raw = future.result()
        outcome = validate_agent_output(raw, snapshot=snapshot, cycle_id=cycle_id)
        if not outcome.accepted or outcome.result is None:
            rejected[index] = {
                "agent_name": name,
                "agent_version": version,
                "reason": outcome.reason or "VALIDATION_FAILED",
                "raw": None if not isinstance(raw, AgentResult) else raw.to_dict(),
            }
            records[index] = AgentDispatchRecord(
                agent_name=name,
                agent_version=version,
                status="REJECTED",
                started_at=started,
                ended_at=ended,
                accepted=False,
                rejection_reason=outcome.reason,
            )
            continue
        accepted[index] = outcome.result
        records[index] = AgentDispatchRecord(
            agent_name=name,
            agent_version=version,
            status=outcome.result.status.value,
            started_at=started,
            ended_at=ended,
            accepted=True,
            execution_time_ms=outcome.result.execution_time_ms,
        )

    for future in pending:
        index = future_to_index[future]
        agent = agents[index]
        name = getattr(agent, "agent_name", type(agent).__name__)
        version = getattr(agent, "agent_version", "unknown")
        # Best-effort only — running callables cannot be killed safely.
        future.cancel()
        ended = _now()
        error = _error_result(name, version, snapshot, cycle_id, reason="TIMEOUT", timeout=True)
        accepted[index] = error
        records[index] = AgentDispatchRecord(
            agent_name=name,
            agent_version=version,
            status=AgentStatus.ERROR.value,
            started_at=started,
            ended_at=ended,
            accepted=True,
            rejection_reason="TIMEOUT",
        )

    accepted_out = tuple(row for row in accepted if row is not None)
    rejected_out = tuple(row for row in rejected if row is not None)
    records_out = tuple(row for row in records if row is not None)
    return accepted_out, rejected_out, records_out


def _now() -> datetime:
    return datetime.now(tz=IST)


def _call_agent(agent: SpecialistAgent, snapshot: AgentMarketSnapshot, cycle_id: str) -> AgentResult:
    agent_input = AgentInput(
        cycle_id=cycle_id,
        snapshot=snapshot,
        agent_config_version=getattr(agent, "agent_version", ""),
    )
    analyze_input = getattr(agent, "analyze_input", None)
    if callable(analyze_input):
        return analyze_input(agent_input)
    try:
        return agent.analyze(snapshot, cycle_id=cycle_id)
    except TypeError:
        return agent.analyze(snapshot)


def _error_result(
    name: str,
    version: str,
    snapshot: AgentMarketSnapshot,
    cycle_id: str,
    *,
    reason: str,
    timeout: bool = False,
) -> AgentResult:
    is_timeout = timeout or reason == "TIMEOUT"
    flags = ("AGENT_TIMEOUT",) if is_timeout else ("AGENT_FAILURE",)
    return AgentResult(
        agent_name=name,
        agent_version=version,
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        decision_timestamp=snapshot.decision_timestamp,
        status=AgentStatus.ERROR,
        observations=(
            f"agent_failure:{reason}",
            *(
                ("fail_closed: timed-out specialist cannot contribute a recommendation",)
                if is_timeout
                else ()
            ),
        ),
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
        invalidation_reason=(
            "TIMEOUT:specialist exceeded timeout" if is_timeout else reason
        ),
        risk_flags=flags,
        missing_data=(),
        confidence=None,
        cycle_id=cycle_id,
    )
