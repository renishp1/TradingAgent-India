"""2D CEO synthesis. Approves the 2C winner or NO_TRADE. Never executes."""

from __future__ import annotations

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
from grow.research.validate import no_trade

PROVIDER = "fixture"
MODEL = "grow.research.fixture.v1"


class CEOAgent:
    agent_id = "ceo"
    agent_version = "v1"

    def __init__(self, prompt_version: str = "v1") -> None:
        self.prompt_version = prompt_version

    def synthesize(self, packet: ResearchPacket, reports: tuple[ResearchReport, ...]) -> CEODecision:
        by_id = {r.agent_id: r for r in reports}
        reasons: list[str] = []
        if packet.options_status != "CANDIDATE" or not packet.option_candidate_id:
            return no_trade(
                packet,
                reasons=("OPTIONS_NO_TRADE",),
                reports=reports,
                prompt_version=self.prompt_version,
            )
        for name in ("bull", "bear", "quant", "risk_context"):
            report = by_id.get(name)
            if report is None:
                reasons.append(f"MISSING_REPORT:{name}")
            elif report.stance is Stance.INSUFFICIENT_DATA:
                reasons.append(f"INSUFFICIENT_DATA:{name}")
        quant = by_id.get("quant")
        risk = by_id.get("risk_context")
        if quant is not None and quant.recommendation is Recommendation.OPPOSE:
            reasons.append("QUANT_OPPOSE")
        if risk is not None and risk.recommendation is Recommendation.OPPOSE:
            reasons.append("RISK_CONTEXT_OPPOSE")
        cand = packet.option_candidate or {}
        direction = packet.strategy_direction
        if direction == "BULLISH" and cand.get("option_type") != "CE":
            reasons.append("BULLISH_NOT_CE")
        if direction == "BEARISH" and cand.get("option_type") != "PE":
            reasons.append("BEARISH_NOT_PE")
        if direction not in {"BULLISH", "BEARISH"}:
            reasons.append(f"INVALID_DIRECTION:{direction}")
        if reasons:
            return no_trade(
                packet,
                reasons=tuple(dict.fromkeys(reasons)),
                reports=reports,
                prompt_version=self.prompt_version,
            )
        support = tuple(r.report_id for r in reports if r.recommendation is Recommendation.SUPPORT)
        conflict = tuple(r.report_id for r in reports if r.recommendation is Recommendation.OPPOSE)
        return CEODecision(
            decision_id=decision_id_for(
                packet_id=packet.packet_id,
                decision=CEOVerdict.TRADE_APPROVE.value,
                candidate_id=packet.option_candidate_id,
                prompt_version=self.prompt_version,
            ),
            packet_id=packet.packet_id,
            decision=CEOVerdict.TRADE_APPROVE,
            selected_candidate_id=packet.option_candidate_id,
            direction=direction,
            rationale_summary=(
                f"Approve 2C winner {packet.option_candidate_id} as BUY {cand.get('option_type')} "
                f"({direction}). Debate preserved."
            ),
            supporting_report_ids=support,
            conflicting_report_ids=conflict,
            confidence=0.5,
            uncertainty_flags=conflict and ("RESEARCH_DISAGREEMENT",) or (),
            rejection_reasons=(),
            approval_basis=(
                f"candidate_id={packet.option_candidate_id}",
                f"option_type={cand.get('option_type')}",
                f"strike={cand.get('strike')}",
                f"expiry={cand.get('expiry')}",
            ),
            model_provider=PROVIDER,
            model_name=MODEL,
            prompt_version=self.prompt_version,
            decision_schema_version=DECISION_SCHEMA,
            as_of=packet.as_of,
            created_at=packet.as_of,
            validation_ok=False,
            validation_failures=(),
        )
