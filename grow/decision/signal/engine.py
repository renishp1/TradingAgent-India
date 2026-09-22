"""Phase 7 — deterministic campaign Signal Engine.

Synthesizes an explained BUY_CE / BUY_PE / NO_TRADE from the 4B package and
the campaign option-chain filter. Counter-evidence is always recorded when the
engine refuses a trade. Risk Guard remains outside this module.
"""

from __future__ import annotations

from typing import Any

from grow.config import GrowConfig, load_config
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.chain_filter import allow_campaign_candidate
from grow.decision.integration.contract import DecisionAction, StrategyCandidate
from grow.decision.signal.models import (
    SIGNAL_ENGINE_VERSION,
    CampaignSignal,
    CounterEvidence,
    build_signal_id,
)
from grow.market_data.normalized.models import AgentMarketSnapshot
from grow.orchestration.models import AggregateAnalysisPackage


_ACTIONABLE = {CandidateAction.PAPER_OPEN}


class SignalEngine:
    """Dedicated Phase-7 signal surface. Deterministic; buyer-only."""

    def __init__(self, config: GrowConfig | None = None) -> None:
        self.config = config if config is not None else load_config()

    def evaluate(
        self,
        *,
        snapshot: AgentMarketSnapshot,
        package: AggregateAnalysisPackage,
    ) -> CampaignSignal:
        """Produce one explained campaign signal for the analysis package."""
        supporting: list[str] = list(package.supporting_evidence)
        counter: list[CounterEvidence] = []
        reasons: list[str] = []

        for item in package.conflicting_evidence:
            counter.append(
                CounterEvidence(code="CONFLICTING_EVIDENCE", source="package", detail=str(item))
            )
        for item in package.debate.conflicts:
            counter.append(CounterEvidence(code="DEBATE_CONFLICT", source="debate", detail=str(item)))
            reasons.append(str(item))
        for name in package.debate.dissenting_agents:
            counter.append(
                CounterEvidence(code="DISSENTING_AGENT", source=str(name), detail="agent dissented")
            )
        for name in package.debate.insufficient_agents:
            counter.append(
                CounterEvidence(code="INSUFFICIENT_AGENT", source=str(name), detail="NO_DATA")
            )
            reasons.append(f"INSUFFICIENT_AGENT:{name}")
        for name in package.debate.error_agents:
            counter.append(CounterEvidence(code="ERROR_AGENT", source=str(name), detail="ERROR"))
            reasons.append(f"ERROR_AGENT:{name}")
        for name in package.unavailable_agents:
            counter.append(
                CounterEvidence(code="UNAVAILABLE_AGENT", source=str(name), detail="unavailable")
            )

        actionable = tuple(
            row
            for row in package.agent_outputs
            if row.status is AgentStatus.PASS and row.candidate_action in _ACTIONABLE
        )
        for row in actionable:
            for finding in row.findings:
                supporting.append(f"{row.agent_name}:finding:{finding}")
            for evidence in row.evidence:
                supporting.append(f"{row.agent_name}:evidence:{evidence}")
            if row.entry_reason:
                supporting.append(f"{row.agent_name}:entry:{row.entry_reason}")

        if not actionable:
            reasons.append("NO_ACTIONABLE_AGENTS")
            counter.append(
                CounterEvidence(
                    code="NO_ACTIONABLE_AGENTS",
                    source="signal_engine",
                    detail="no PASS agent proposed PAPER_OPEN",
                )
            )
            return self._close(
                snapshot=snapshot,
                package=package,
                supporting=supporting,
                counter=counter,
                reasons=reasons,
                explanation="No actionable PASS/PAPER_OPEN agent output; fail closed to NO_TRADE.",
            )

        if not package.debate.agreement or package.debate.conflicts:
            if "DEBATE_DISAGREEMENT" not in reasons:
                reasons.append("DEBATE_DISAGREEMENT")
            counter.append(
                CounterEvidence(
                    code="DEBATE_DISAGREEMENT",
                    source="debate",
                    detail="agents did not reach a conflict-free agreement",
                )
            )
            return self._close(
                snapshot=snapshot,
                package=package,
                supporting=supporting,
                counter=counter,
                reasons=reasons,
                explanation="Debate disagreement or conflicts; fail closed to NO_TRADE.",
            )

        primary = actionable[0]
        candidate = _candidate_from_agent(primary)
        if candidate is None:
            reasons.append("INCOMPLETE_SIGNAL_CANDIDATE")
            counter.append(
                CounterEvidence(
                    code="INCOMPLETE_SIGNAL_CANDIDATE",
                    source=primary.agent_name,
                    detail="missing direction/instrument/limit/stop for signal",
                )
            )
            return self._close(
                snapshot=snapshot,
                package=package,
                supporting=supporting,
                counter=counter,
                reasons=reasons,
                explanation="Actionable agent lacked complete candidate fields; NO_TRADE.",
            )

        reject, filtered = allow_campaign_candidate(snapshot, candidate, config=self.config)
        if reject is not None:
            reasons.append(reject)
            counter.append(
                CounterEvidence(
                    code="CHAIN_FILTER",
                    source="option_chain_filter",
                    detail=reject,
                )
            )
            for code in filtered.reason_codes:
                if code not in reasons:
                    reasons.append(code)
            return self._close(
                snapshot=snapshot,
                package=package,
                supporting=supporting,
                counter=counter,
                reasons=reasons,
                explanation=f"Option-chain filter rejected candidate ({reject}); NO_TRADE.",
                direction=candidate.direction,
                underlying=candidate.underlying,
                instrument=candidate.instrument,
                option_type=candidate.option_type,
            )

        option_type = candidate.option_type
        if option_type == "CE":
            action = DecisionAction.BUY_CE
        elif option_type == "PE":
            action = DecisionAction.BUY_PE
        else:
            reasons.append("UNSUPPORTED_OPTION_TYPE")
            counter.append(
                CounterEvidence(
                    code="UNSUPPORTED_OPTION_TYPE",
                    source="signal_engine",
                    detail=str(option_type),
                )
            )
            return self._close(
                snapshot=snapshot,
                package=package,
                supporting=supporting,
                counter=counter,
                reasons=reasons,
                explanation="Candidate option_type is not CE/PE; NO_TRADE.",
                direction=candidate.direction,
                underlying=candidate.underlying,
                instrument=candidate.instrument,
            )

        supporting.append(f"chain_filter:eligible={len(filtered.eligible_instruments)}")
        if filtered.selected_expiry:
            supporting.append(f"chain_filter:expiry={filtered.selected_expiry}")
        explanation = (
            f"Agreed PAPER_OPEN for {candidate.instrument} ({option_type}); "
            f"chain filter allowlisted the contract → {action.value}."
        )
        identity = {
            "engine": SIGNAL_ENGINE_VERSION,
            "package_digest": package.package_digest,
            "snapshot_id": snapshot.snapshot_id,
            "action": action.value,
            "instrument": candidate.instrument,
            "reasons": reasons,
            "counter": [row.to_dict() for row in counter],
            "supporting": supporting,
        }
        return CampaignSignal(
            signal_id=build_signal_id(identity),
            action=action,
            direction=candidate.direction,
            underlying=candidate.underlying,
            instrument=candidate.instrument,
            option_type=option_type,
            explanation=explanation,
            supporting_evidence=tuple(dict.fromkeys(supporting)),
            counter_evidence=tuple(counter),
            reason_codes=(),
            package_digest=package.package_digest,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            analysis_cycle_id=package.cycle_id,
        )

    def _close(
        self,
        *,
        snapshot: AgentMarketSnapshot,
        package: AggregateAnalysisPackage,
        supporting: list[str],
        counter: list[CounterEvidence],
        reasons: list[str],
        explanation: str,
        direction: str | None = None,
        underlying: str | None = None,
        instrument: str | None = None,
        option_type: str | None = None,
    ) -> CampaignSignal:
        uniq_reasons = tuple(dict.fromkeys(reasons))
        identity = {
            "engine": SIGNAL_ENGINE_VERSION,
            "package_digest": package.package_digest,
            "snapshot_id": snapshot.snapshot_id,
            "action": DecisionAction.NO_TRADE.value,
            "reasons": list(uniq_reasons),
            "counter": [row.to_dict() for row in counter],
        }
        return CampaignSignal(
            signal_id=build_signal_id(identity),
            action=DecisionAction.NO_TRADE,
            direction=direction,
            underlying=underlying,
            instrument=instrument,
            option_type=option_type,
            explanation=explanation,
            supporting_evidence=tuple(dict.fromkeys(supporting)),
            counter_evidence=tuple(counter),
            reason_codes=uniq_reasons,
            package_digest=package.package_digest,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            analysis_cycle_id=package.cycle_id,
        )


def _parse_float(value: Any) -> float | None:
    """Parse an untrusted float. Fail closed on bool/None/malformed values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _parse_int(value: Any) -> int | None:
    """Parse an untrusted int. Fail closed on bool/None/malformed values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, float):
            if not value.is_integer():
                return None
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            return int(text)
        return int(value)
    except (TypeError, ValueError):
        return None


def _candidate_from_agent(row: AgentResult) -> StrategyCandidate | None:
    """Build a StrategyCandidate from agent metrics. All metric parsing fails closed."""
    metrics = dict(row.calculated_metrics or {})
    direction = str(metrics.get("direction") or "").strip().upper()
    instrument = (row.candidate_instrument or "").strip()
    underlying = str(metrics.get("underlying") or "").strip().upper()
    if not instrument or direction not in {"BULLISH", "BEARISH"}:
        return None
    if not underlying:
        underlying = instrument.split("-")[0].upper()

    if "limit_price" not in metrics or "stop_loss" not in metrics:
        return None
    limit_price = _parse_float(metrics.get("limit_price"))
    stop_loss = _parse_float(metrics.get("stop_loss"))
    if limit_price is None or stop_loss is None:
        return None

    if "quantity" in metrics:
        quantity = _parse_int(metrics.get("quantity"))
    elif "lots" in metrics:
        quantity = _parse_int(metrics.get("lots"))
    else:
        return None
    if quantity is None or quantity < 1:
        return None

    strike_raw = metrics.get("strike")
    if strike_raw is None:
        strike: float | None = None
    else:
        strike = _parse_float(strike_raw)
        if strike is None:
            return None

    target_raw = metrics.get("target")
    if target_raw is None:
        target: float | None = None
    else:
        target = _parse_float(target_raw)
        if target is None:
            return None

    lot_size_raw = metrics.get("lot_size")
    if lot_size_raw is None:
        lot_size: int | None = None
    else:
        lot_size = _parse_int(lot_size_raw)
        if lot_size is None:
            return None

    option_type = metrics.get("option_type")
    if option_type is None and instrument:
        token = instrument.upper().split("-")[-1]
        if token in {"CE", "PE"}:
            option_type = token

    expiry = metrics.get("expiry")
    confidence = 0.0
    if row.confidence is not None:
        parsed_confidence = _parse_float(row.confidence)
        if parsed_confidence is None:
            return None
        confidence = parsed_confidence

    return StrategyCandidate(
        strategy=str(metrics.get("strategy") or row.agent_name),
        instrument=instrument,
        underlying=underlying,
        direction=direction,
        limit_price=limit_price,
        stop_loss=stop_loss,
        quantity=quantity,
        confidence=confidence,
        option_type=None if option_type is None else str(option_type).upper(),
        strike=strike,
        expiry=None if expiry is None else str(expiry),
        target=target,
        lot_size=lot_size,
    )
