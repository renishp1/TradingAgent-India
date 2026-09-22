"""Validate 4B outputs and select at most one paper candidate.

Mismatched outputs are rejected and cannot support a decision. Conflicts are
preserved. Incomplete evidence is not promoted to a candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.contract import AgentOutputRef, StrategyCandidate, plain_data
from grow.live_data.health import MARKET_DATA_NOT_HEALTHY, reject_unhealthy_market_data
from grow.market_data.normalized.models import AgentMarketSnapshot, DataQualityStatus
from grow.market_data.provenance import MIXED_MARKET_DATA_SOURCE, reject_mixed_market_data
from grow.market_data.snapshots.builder import gate_snapshot_quality
from grow.orchestration.models import AggregateAnalysisPackage
from grow.orchestration.validator import validate_agent_output


_ACTIONABLE = {CandidateAction.PAPER_OPEN}
_BULLISH = ("BULL", "TRENDING_UP", "SMA_FAST_ABOVE")
_BEARISH = ("BEAR", "TRENDING_DOWN", "SMA_FAST_BELOW")
_ASSUMPTION = (
    "Risk Guard limits are not modified by agent outputs.",
    "CANDIDATE is a paper-trade candidate and is not a live or broker order.",
)


@dataclass(frozen=True)
class PolicyResult:
    terminal_status: str | None
    reason_codes: tuple[str, ...]
    candidate: StrategyCandidate | None
    refs: tuple[AgentOutputRef, ...]
    observations: tuple[str, ...]
    calculated_evidence: dict[str, Any]
    supporting_findings: tuple[str, ...]
    conflicting_findings: tuple[str, ...]
    assumptions: tuple[str, ...]
    gates: tuple[tuple[str, bool, str], ...]
    data_quality: str
    regime_high_volatility: bool


def classify_output(
    result: Any,
    *,
    snapshot: AgentMarketSnapshot,
    cycle_id: str,
) -> tuple[bool, str | None, AgentResult | None]:
    """Reject schema, snapshot, or cycle mismatches. Those outputs are unusable."""
    if not isinstance(result, AgentResult):
        return False, "SCHEMA_MISMATCH", None
    outcome = validate_agent_output(result, snapshot=snapshot, cycle_id=cycle_id)
    return outcome.accepted, outcome.reason, outcome.result


def evaluate_policy(
    snapshot: AgentMarketSnapshot,
    package: AggregateAnalysisPackage,
) -> PolicyResult:
    gates: list[tuple[str, bool, str]] = []
    mixed = reject_mixed_market_data(snapshot)
    if mixed:
        gates.append(("market_data_source", False, mixed))
        return _closed(
            terminal="BLOCKED",
            reasons=(mixed,),
            gates=tuple(gates),
            quality=snapshot.data_quality.value,
            package=package,
            observations=tuple(snapshot.quality_notes),
        )
    gates.append(
        (
            "market_data_source",
            True,
            getattr(snapshot.market_data_source, "value", str(snapshot.market_data_source)),
        )
    )
    quality = gate_snapshot_quality(snapshot)
    gates.append(("data_quality", quality is DataQualityStatus.OK, quality.value))
    if quality is not DataQualityStatus.OK:
        code = {
            DataQualityStatus.STALE: "DATA_STALE",
            DataQualityStatus.INSUFFICIENT: "DATA_INSUFFICIENT",
            DataQualityStatus.REJECTED: "DATA_REJECTED",
            DataQualityStatus.DEGRADED: "DATA_DEGRADED",
        }.get(quality, "DATA_UNUSABLE")
        if MIXED_MARKET_DATA_SOURCE in snapshot.quality_notes:
            code = MIXED_MARKET_DATA_SOURCE
        terminal = "BLOCKED" if quality is DataQualityStatus.REJECTED else "NO_TRADE"
        return _closed(
            terminal=terminal,
            reasons=(code,),
            gates=tuple(gates),
            quality=quality.value,
            package=package,
            observations=tuple(snapshot.quality_notes),
        )
    unhealthy = reject_unhealthy_market_data(
        data_quality=quality,
        diagnostics=dict(snapshot.diagnostics or {}),
        freshness_ok=bool((snapshot.diagnostics or {}).get("freshness_ok", True)),
    )
    if unhealthy:
        gates.append(("market_data_health", False, unhealthy))
        return _closed(
            terminal="BLOCKED",
            reasons=(unhealthy,),
            gates=tuple(gates),
            quality=quality.value,
            package=package,
            observations=tuple(snapshot.quality_notes),
        )
    gates.append(
        (
            "market_data_health",
            True,
            str((snapshot.diagnostics or {}).get("market_data_health") or "HEALTHY"),
        )
    )

    accepted: list[AgentResult] = []
    refs: list[AgentOutputRef] = []
    for item in package.rejected_outputs:
        refs.append(_rejected_ref(item, package))
    schema_rejects: list[str] = []
    for raw in package.agent_outputs:
        ok, reason, stamped = classify_output(raw, snapshot=snapshot, cycle_id=package.cycle_id)
        if not ok or stamped is None:
            schema_rejects.append(reason or "SCHEMA_MISMATCH")
            refs.append(_raw_ref(raw, package, reason or "SCHEMA_MISMATCH"))
            continue
        accepted.append(stamped)
        refs.append(
            AgentOutputRef(
                agent_name=stamped.agent_name,
                agent_version=stamped.agent_version,
                cycle_id=stamped.cycle_id,
                snapshot_id=stamped.snapshot_id,
                snapshot_version=stamped.snapshot_version,
                status=stamped.status.value,
                relied_upon=False,
            )
        )

    if schema_rejects or package.rejected_outputs:
        gates.append(("output_validation", False, ",".join(schema_rejects) or "REJECTED_OUTPUT"))
        return _closed(
            terminal="NO_TRADE",
            reasons=("REJECTED_OUTPUT", *schema_rejects),
            gates=tuple(gates),
            quality=quality.value,
            package=package,
            refs=tuple(refs),
            conflicting=_conflict_strings(package, tuple(accepted)),
        )

    gates.append(("output_validation", True, f"accepted={len(accepted)}"))
    conflicts = _conflict_strings(package, tuple(accepted))
    if conflicts:
        gates.append(("conflicts", False, conflicts[0]))
        return _closed(
            terminal="NO_TRADE",
            reasons=("AGENT_CONFLICT", *conflicts),
            gates=tuple(gates),
            quality=quality.value,
            package=package,
            refs=tuple(refs),
            accepted=tuple(accepted),
            conflicting=conflicts,
        )
    gates.append(("conflicts", True, "none"))

    errors = tuple(row.agent_name for row in accepted if row.status is AgentStatus.ERROR)
    unavailable = tuple(dict.fromkeys((*package.unavailable_agents, *errors)))
    no_data = tuple(row.agent_name for row in accepted if row.status is AgentStatus.NO_DATA)
    if errors or unavailable or no_data:
        gates.append(("evidence_complete", False, ",".join(unavailable or no_data)))
        return _closed(
            terminal="NO_TRADE",
            reasons=("INCOMPLETE_EVIDENCE",),
            gates=tuple(gates),
            quality=quality.value,
            package=package,
            refs=tuple(refs),
            accepted=tuple(accepted),
        )
    gates.append(("evidence_complete", True, "complete"))

    actionable = [
        row
        for row in accepted
        if row.status is AgentStatus.PASS and row.candidate_action in _ACTIONABLE
    ]
    if not actionable:
        gates.append(("strategy_candidate", False, "none"))
        return _closed(
            terminal="NO_TRADE",
            reasons=("NO_VALID_STRATEGY_CANDIDATE",),
            gates=tuple(gates),
            quality=quality.value,
            package=package,
            refs=tuple(refs),
            accepted=tuple(accepted),
        )

    candidate, candidate_reasons = _candidate_from(actionable)
    if candidate is None:
        gates.append(("strategy_candidate", False, candidate_reasons[0]))
        return _closed(
            terminal="NO_TRADE",
            reasons=candidate_reasons,
            gates=tuple(gates),
            quality=quality.value,
            package=package,
            refs=tuple(refs),
            accepted=tuple(accepted),
            conflicting=tuple(code for code in candidate_reasons if "CONFLICT" in code),
        )

    stale = _stale_instrument(snapshot, candidate.instrument)
    if stale:
        gates.append(("instrument_quality", False, stale))
        return _closed(
            terminal="NO_TRADE",
            reasons=(stale,),
            gates=tuple(gates),
            quality=quality.value,
            package=package,
            refs=tuple(refs),
            accepted=tuple(accepted),
        )
    gates.append(("instrument_quality", True, candidate.instrument))
    gates.append(("strategy_candidate", True, candidate.strategy))

    relied = {row.agent_name for row in actionable}
    marked: list[AgentOutputRef] = []
    for row in refs:
        if row.agent_name in relied and row.rejection_reason is None:
            marked.append(
                AgentOutputRef(
                    agent_name=row.agent_name,
                    agent_version=row.agent_version,
                    cycle_id=row.cycle_id,
                    snapshot_id=row.snapshot_id,
                    snapshot_version=row.snapshot_version,
                    status=row.status,
                    relied_upon=True,
                )
            )
        else:
            marked.append(row)
    observations, calculated, supporting, assumptions = _evidence(tuple(accepted), relied)
    return PolicyResult(
        terminal_status=None,
        reason_codes=(),
        candidate=candidate,
        refs=tuple(marked),
        observations=observations,
        calculated_evidence=calculated,
        supporting_findings=supporting,
        conflicting_findings=(),
        assumptions=assumptions,
        gates=tuple(gates),
        data_quality=quality.value,
        regime_high_volatility=_high_volatility(tuple(accepted)),
    )


def _closed(
    *,
    terminal: str,
    reasons: tuple[str, ...],
    gates: tuple[tuple[str, bool, str], ...],
    quality: str,
    package: AggregateAnalysisPackage,
    refs: tuple[AgentOutputRef, ...] = (),
    accepted: tuple[AgentResult, ...] = (),
    observations: tuple[str, ...] = (),
    conflicting: tuple[str, ...] = (),
) -> PolicyResult:
    if not refs:
        refs = tuple(_rejected_ref(item, package) for item in package.rejected_outputs)
    relied: set[str] = set()
    obs, calculated, supporting, assumptions = _evidence(accepted, relied)
    if observations:
        obs = observations + obs
    return PolicyResult(
        terminal_status=terminal,
        reason_codes=tuple(dict.fromkeys(reasons)),
        candidate=None,
        refs=refs,
        observations=obs,
        calculated_evidence=calculated,
        supporting_findings=supporting,
        conflicting_findings=tuple(dict.fromkeys(conflicting)),
        assumptions=assumptions,
        gates=gates,
        data_quality=quality,
        regime_high_volatility=False,
    )


def _evidence(
    accepted: tuple[AgentResult, ...],
    relied: set[str],
) -> tuple[tuple[str, ...], dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    observations: list[str] = []
    calculated: dict[str, Any] = {}
    supporting: list[str] = []
    assumptions: list[str] = list(_ASSUMPTION)
    for row in accepted:
        if row.status is AgentStatus.ERROR:
            continue
        observations.extend(f"{row.agent_name}:{item}" for item in row.observations)
        calculated[row.agent_name] = plain_data(dict(row.calculated_metrics))
        if row.agent_name in relied or not relied:
            supporting.extend(f"{row.agent_name}:{item}" for item in row.findings)
        assumptions.extend(row.assumptions)
    if not relied:
        supporting = []
    else:
        supporting = [item for item in supporting if item.split(":", 1)[0] in relied]
    return tuple(observations), calculated, tuple(supporting), tuple(dict.fromkeys(assumptions))


def _conflict_strings(
    package: AggregateAnalysisPackage,
    accepted: tuple[AgentResult, ...],
) -> tuple[str, ...]:
    found: list[str] = list(package.conflicts) + list(package.debate.conflicts)
    actionable = [
        row
        for row in accepted
        if row.status is AgentStatus.PASS and row.candidate_action in _ACTIONABLE
    ]
    actions = sorted({row.candidate_action.value for row in actionable})
    instruments = sorted({row.candidate_instrument or "" for row in actionable})
    if len(actions) > 1:
        found.append("ACTION_CONFLICT:" + ",".join(actions))
    if len([item for item in instruments if item]) > 1:
        found.append("INSTRUMENT_CONFLICT:" + ",".join(item for item in instruments if item))
    bullish: list[str] = []
    bearish: list[str] = []
    for row in accepted:
        if row.status is AgentStatus.ERROR:
            continue
        blob = " ".join(row.findings + row.interpretation).upper()
        if any(token in blob for token in _BULLISH):
            bullish.append(row.agent_name)
        if any(token in blob for token in _BEARISH):
            bearish.append(row.agent_name)
    if bullish and bearish:
        found.append(
            "DIRECTION_CONFLICT:bullish="
            + ",".join(bullish)
            + ";bearish="
            + ",".join(bearish)
        )
    return tuple(dict.fromkeys(found))


def _candidate_from(
    actionable: list[AgentResult],
) -> tuple[StrategyCandidate | None, tuple[str, ...]]:
    merged: dict[str, Any] = {}
    conflicts: list[str] = []
    keys = ("strategy", "direction", "underlying", "limit_price", "stop_loss", "quantity")
    for row in actionable:
        for key in keys:
            if key not in row.calculated_metrics:
                continue
            value = row.calculated_metrics[key]
            if key not in merged:
                merged[key] = value
            elif merged[key] != value:
                conflicts.append(f"EVIDENCE_CONFLICT:{key}")
    if conflicts:
        return None, ("AGENT_CONFLICT", *tuple(dict.fromkeys(conflicts)))

    instrument = actionable[0].candidate_instrument
    if not instrument or any(row.candidate_instrument != instrument for row in actionable):
        return None, ("NO_VALID_STRATEGY_CANDIDATE",)
    strategy = merged.get("strategy")
    direction = merged.get("direction")
    underlying = merged.get("underlying")
    if not isinstance(strategy, str) or not strategy.strip():
        return None, ("NO_VALID_STRATEGY_CANDIDATE",)
    if direction not in {"BULLISH", "BEARISH"}:
        return None, ("NO_VALID_STRATEGY_CANDIDATE",)
    if not isinstance(underlying, str) or not underlying.strip():
        return None, ("INCOMPLETE_CANDIDATE",)
    limit = _as_float(merged.get("limit_price"))
    stop = _as_float(merged.get("stop_loss"))
    quantity = merged.get("quantity")
    if limit is None or limit <= 0 or stop is None or stop <= 0:
        return None, ("INCOMPLETE_CANDIDATE",)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        return None, ("INCOMPLETE_CANDIDATE",)
    suffix_conflict = _suffix_conflict(instrument, str(direction))
    if suffix_conflict:
        return None, ("AGENT_CONFLICT", suffix_conflict)
    confidences = [row.confidence for row in actionable if row.confidence is not None]
    confidence = min(confidences) if confidences else 0.0
    return (
        StrategyCandidate(
            strategy=strategy.strip(),
            instrument=instrument,
            underlying=underlying.strip().upper(),
            direction=str(direction),
            limit_price=limit,
            stop_loss=stop,
            quantity=quantity,
            confidence=confidence,
        ),
        (),
    )


def _suffix_conflict(instrument: str, direction: str) -> str | None:
    upper = instrument.upper()
    if upper.endswith("-CE") and direction == "BEARISH":
        return "DIRECTION_INSTRUMENT_CONFLICT:BEARISH_CE"
    if upper.endswith("-PE") and direction == "BULLISH":
        return "DIRECTION_INSTRUMENT_CONFLICT:BULLISH_PE"
    return None


def _stale_instrument(snapshot: AgentMarketSnapshot, instrument: str) -> str | None:
    upper = instrument.upper()
    if not (upper.endswith("-CE") or upper.endswith("-PE")):
        return None
    matches = [
        row
        for row in snapshot.option_contracts
        if instrument in {row.provider_contract_id, f"{row.underlying}-{row.strike:g}-{row.option_type}"}
        or upper == f"{row.underlying}-{row.strike:g}-{row.option_type}".upper()
    ]
    if not matches:
        return "CRITICAL_DATA_MISSING"
    if any(row.quality is not DataQualityStatus.OK or row.ltp is None for row in matches):
        return "DATA_STALE"
    return None


def _high_volatility(accepted: tuple[AgentResult, ...]) -> bool:
    for row in accepted:
        blob = " ".join(row.findings + row.interpretation + row.risk_flags).upper()
        if "HIGH_VOLATILITY" in blob or "HIGH VOLATILITY" in blob:
            return True
    return False


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rejected_ref(item: dict[str, Any], package: AggregateAnalysisPackage) -> AgentOutputRef:
    return AgentOutputRef(
        agent_name=str(item.get("agent_name", "unknown")),
        agent_version=str(item.get("agent_version", "unknown")),
        cycle_id=package.cycle_id,
        snapshot_id=str(item.get("snapshot_id", package.snapshot_id)),
        snapshot_version=str(item.get("snapshot_version", package.snapshot_version)),
        status="REJECTED",
        relied_upon=False,
        rejection_reason=str(item.get("reason", "REJECTED")),
    )


def _raw_ref(raw: Any, package: AggregateAnalysisPackage, reason: str) -> AgentOutputRef:
    if isinstance(raw, AgentResult):
        return AgentOutputRef(
            agent_name=raw.agent_name,
            agent_version=raw.agent_version,
            cycle_id=raw.cycle_id or package.cycle_id,
            snapshot_id=raw.snapshot_id,
            snapshot_version=raw.snapshot_version,
            status=raw.status.value,
            relied_upon=False,
            rejection_reason=reason,
        )
    return AgentOutputRef(
        agent_name="unknown",
        agent_version="unknown",
        cycle_id=package.cycle_id,
        snapshot_id=package.snapshot_id,
        snapshot_version=package.snapshot_version,
        status="REJECTED",
        relied_upon=False,
        rejection_reason=reason,
    )
