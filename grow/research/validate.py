"""Deterministic CEO decision gate. Fail closed to NO_TRADE."""

from __future__ import annotations

from typing import Any, Mapping

from grow.config import AIConfig
from grow.research.models import (
    DECISION_SCHEMA,
    CEODecision,
    CEOVerdict,
    Recommendation,
    ResearchPacket,
    ResearchReport,
    Stance,
    decision_id_for,
)

_PROHIBITED = frozenset(
    {
        "quantity",
        "order",
        "broker",
        "execute",
        "sell",
        "short",
        "place_order",
        "submit_order",
        "ledger",
        "bypass",
        "buy",
        "option_sell",
    }
)
_REQUIRED_AGENTS = ("bull", "bear", "quant", "risk_context")
_INTEGRITY = (
    "PACKET_SCHEMA",
    "DECISION_SCHEMA",
    "PACKET_ID_MISMATCH",
    "ASOF_MISMATCH",
    "SNAPSHOT_INCONSISTENT",
    "PROHIBITED_FIELD",
    "EXECUTION_LANGUAGE",
    "NO_CANDIDATE",
    "MISSING_CANDIDATE_ID",
    "UNKNOWN_CANDIDATE",
    "CANDIDATE_SNAPSHOT_MISMATCH",
    "NEUTRAL_WITH_CANDIDATE",
    "BULLISH_PE",
    "BEARISH_CE",
    "BULLISH_NOT_CE",
    "BEARISH_NOT_PE",
    "NON_BUY_INTENT",
    "CANDIDATE_MUTATION",
    "MISSING_REPORT",
    "REPORT_PACKET_MISMATCH",
    "REPORT_ASOF_MISMATCH",
    "CEO_CONFIDENCE_INVALID",
    "INVALID_DIRECTION",
)


def _is_integrity(reason: str) -> bool:
    return any(reason == prefix or reason.startswith(prefix + ":") for prefix in _INTEGRITY)


def asof_failures(packet: ResearchPacket) -> tuple[str, ...]:
    expected = packet.as_of.isoformat()
    view_asof = packet.market_research_view.get("as_of")
    signal_asof = packet.strategy_evidence.get("as_of")
    if view_asof != expected or signal_asof != expected:
        return ("ASOF_MISMATCH",)
    candidate = packet.option_candidate
    if candidate is not None and candidate.get("as_of") not in {None, expected}:
        return ("ASOF_MISMATCH",)
    return ()


def _scan_prohibited(payload: Any, found: list[str]) -> None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            lowered = str(key).lower()
            if lowered in _PROHIBITED:
                found.append(f"PROHIBITED_FIELD:{lowered}")
            _scan_prohibited(value, found)
        return
    if isinstance(payload, (list, tuple, set)):
        for item in payload:
            _scan_prohibited(item, found)


def no_trade(
    packet: ResearchPacket,
    *,
    reasons: tuple[str, ...],
    reports: tuple[ResearchReport, ...] = (),
    prompt_version: str = "v1",
    provider: str = "fixture",
    model: str = "grow.research.fixture.v1",
    extra_failures: tuple[str, ...] = (),
    validation_ok: bool = True,
) -> CEODecision:
    decision = CEOVerdict.NO_TRADE
    return CEODecision(
        decision_id=decision_id_for(
            packet_id=packet.packet_id,
            decision=decision.value,
            candidate_id=None,
            prompt_version=prompt_version,
        ),
        packet_id=packet.packet_id,
        decision=decision,
        selected_candidate_id=None,
        direction="NEUTRAL",
        rationale_summary="; ".join(reasons) or "NO_TRADE",
        supporting_report_ids=tuple(r.report_id for r in reports if r.recommendation is Recommendation.SUPPORT),
        conflicting_report_ids=tuple(r.report_id for r in reports if r.recommendation is Recommendation.OPPOSE),
        confidence=0.0,
        uncertainty_flags=reasons,
        rejection_reasons=reasons,
        approval_basis=(),
        model_provider=provider,
        model_name=model,
        prompt_version=prompt_version,
        decision_schema_version=DECISION_SCHEMA,
        as_of=packet.as_of,
        created_at=packet.as_of,
        validation_ok=validation_ok,
        validation_failures=extra_failures,
    )


class DecisionValidator:
    def __init__(self, ai: AIConfig | None = None) -> None:
        self.ai = ai

    def validate(
        self,
        packet: ResearchPacket,
        decision: CEODecision,
        reports: tuple[ResearchReport, ...],
        raw: Mapping[str, Any] | None = None,
    ) -> CEODecision:
        failures = list(decision.validation_failures)
        failures.extend(asof_failures(packet))
        if packet.packet_schema_version != "research.packet.v1":
            failures.append("PACKET_SCHEMA")
        if decision.decision_schema_version != DECISION_SCHEMA:
            failures.append("DECISION_SCHEMA")
        if decision.packet_id != packet.packet_id:
            failures.append("PACKET_ID_MISMATCH")
        if decision.as_of != packet.as_of:
            failures.append("ASOF_MISMATCH")
        ids = packet.data_snapshot_ids
        if ids.get("market") != ids.get("signal"):
            failures.append("SNAPSHOT_INCONSISTENT")
        prohibited: list[str] = []
        _scan_prohibited(decision.to_dict(), prohibited)
        if raw is not None:
            _scan_prohibited(raw, prohibited)
        failures.extend(prohibited)
        if decision.direction in {"SELL", "SHORT", "BUY", "LONG", "OPTION_SELL"}:
            failures.append(f"EXECUTION_LANGUAGE:{decision.direction}")
        try:
            confidence = float(decision.confidence)
        except (TypeError, ValueError):
            confidence = float("nan")
            failures.append("CEO_CONFIDENCE_INVALID")
        else:
            if not (0.0 <= confidence <= 1.0):
                failures.append("CEO_CONFIDENCE_INVALID")
        if decision.decision is CEOVerdict.TRADE_APPROVE:
            if packet.options_status != "CANDIDATE" or packet.option_candidate is None:
                failures.append("NO_CANDIDATE")
            if not decision.selected_candidate_id:
                failures.append("MISSING_CANDIDATE_ID")
            elif decision.selected_candidate_id != packet.option_candidate_id:
                failures.append("UNKNOWN_CANDIDATE")
            cand = packet.option_candidate or {}
            if cand.get("underlying_snapshot_id") and cand.get("underlying_snapshot_id") != ids.get("market"):
                failures.append("CANDIDATE_SNAPSHOT_MISMATCH")
            if decision.direction not in {"BULLISH", "BEARISH"}:
                failures.append(f"INVALID_DIRECTION:{decision.direction}")
            if decision.direction == "NEUTRAL":
                failures.append("NEUTRAL_WITH_CANDIDATE")
            option_type = cand.get("option_type")
            if decision.direction == "BULLISH" and option_type != "CE":
                failures.append("BULLISH_NOT_CE")
            if decision.direction == "BEARISH" and option_type != "PE":
                failures.append("BEARISH_NOT_PE")
            if decision.direction == "BULLISH" and option_type == "PE":
                failures.append("BULLISH_PE")
            if decision.direction == "BEARISH" and option_type == "CE":
                failures.append("BEARISH_CE")
            if cand.get("intent") not in {None, "BUY"}:
                failures.append("NON_BUY_INTENT")
            for key in ("strike", "expiry", "premium_reference", "option_type"):
                if raw and key in raw and cand and raw[key] != cand.get(key):
                    failures.append(f"CANDIDATE_MUTATION:{key}")
        by_id = {r.agent_id: r for r in reports}
        for name in _REQUIRED_AGENTS:
            report = by_id.get(name)
            if report is None:
                failures.append(f"MISSING_REPORT:{name}")
                continue
            if report.stance is Stance.INSUFFICIENT_DATA:
                failures.append(f"INSUFFICIENT_DATA:{name}")
            if report.packet_id != packet.packet_id:
                failures.append(f"REPORT_PACKET_MISMATCH:{name}")
            if report.as_of != packet.as_of:
                failures.append(f"REPORT_ASOF_MISMATCH:{name}")
        quant = by_id.get("quant")
        risk = by_id.get("risk_context")
        if quant is not None and quant.recommendation is Recommendation.OPPOSE:
            failures.append("QUANT_OPPOSE")
        if risk is not None and risk.recommendation is Recommendation.OPPOSE:
            failures.append("RISK_CONTEXT_OPPOSE")
        if packet.options_status != "CANDIDATE":
            failures.append("OPTIONS_NO_TRADE")
        ai = self.ai
        if (
            ai is not None
            and ai.confidence_enabled
            and decision.decision is CEOVerdict.TRADE_APPROVE
            and decision.confidence < ai.min_ceo_confidence
        ):
            failures.append("CEO_CONFIDENCE")
        unique = tuple(dict.fromkeys(failures))
        integrity = tuple(item for item in unique if _is_integrity(item))
        if integrity or (unique and decision.decision is CEOVerdict.TRADE_APPROVE):
            return no_trade(
                packet,
                reasons=unique,
                reports=reports,
                prompt_version=decision.prompt_version,
                provider=decision.model_provider,
                model=decision.model_name,
                extra_failures=unique,
                validation_ok=False,
            )
        if unique:
            return CEODecision(
                decision_id=decision.decision_id,
                packet_id=decision.packet_id,
                decision=CEOVerdict.NO_TRADE,
                selected_candidate_id=None,
                direction=decision.direction if decision.decision is CEOVerdict.NO_TRADE else "NEUTRAL",
                rationale_summary=decision.rationale_summary,
                supporting_report_ids=decision.supporting_report_ids,
                conflicting_report_ids=decision.conflicting_report_ids,
                confidence=decision.confidence,
                uncertainty_flags=decision.uncertainty_flags,
                rejection_reasons=decision.rejection_reasons or unique,
                approval_basis=(),
                model_provider=decision.model_provider,
                model_name=decision.model_name,
                prompt_version=decision.prompt_version,
                decision_schema_version=DECISION_SCHEMA,
                as_of=decision.as_of,
                created_at=decision.created_at,
                validation_ok=True,
                validation_failures=(),
            )
        return CEODecision(
            decision_id=decision.decision_id,
            packet_id=decision.packet_id,
            decision=decision.decision,
            selected_candidate_id=decision.selected_candidate_id,
            direction=decision.direction,
            rationale_summary=decision.rationale_summary,
            supporting_report_ids=decision.supporting_report_ids,
            conflicting_report_ids=decision.conflicting_report_ids,
            confidence=decision.confidence,
            uncertainty_flags=decision.uncertainty_flags,
            rejection_reasons=decision.rejection_reasons,
            approval_basis=decision.approval_basis,
            model_provider=decision.model_provider,
            model_name=decision.model_name,
            prompt_version=decision.prompt_version,
            decision_schema_version=DECISION_SCHEMA,
            as_of=decision.as_of,
            created_at=decision.created_at,
            validation_ok=True,
            validation_failures=(),
        )
