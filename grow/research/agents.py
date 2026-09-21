"""Fixture research agents. Packet evidence only. No tools. No network."""

from __future__ import annotations

from typing import Protocol

from grow.research.models import (
    REPORT_SCHEMA,
    Recommendation,
    ResearchPacket,
    ResearchReport,
    Stance,
    report_id_for,
)

PROVIDER = "fixture"
MODEL = "grow.research.fixture.v1"
AGENT_VERSION = "v1"


class ResearchAgent(Protocol):
    agent_id: str
    agent_version: str
    prompt_version: str

    def research(self, packet: ResearchPacket) -> ResearchReport: ...


def _report(
    packet: ResearchPacket,
    *,
    agent_id: str,
    prompt_version: str,
    stance: Stance,
    recommendation: Recommendation,
    confidence: float,
    support: tuple[str, ...],
    contra: tuple[str, ...],
    risks: tuple[str, ...] = (),
    quality: tuple[str, ...] = (),
    assessment: str,
    summary: str,
) -> ResearchReport:
    conf = 0.0 if confidence < 0 else 1.0 if confidence > 1 else round(confidence, 4)
    return ResearchReport(
        report_id=report_id_for(
            packet_id=packet.packet_id,
            agent_id=agent_id,
            prompt_version=prompt_version,
            stance=stance.value,
            recommendation=recommendation.value,
        ),
        agent_id=agent_id,
        agent_version=AGENT_VERSION,
        prompt_version=prompt_version,
        model_provider=PROVIDER,
        model_name=MODEL,
        packet_id=packet.packet_id,
        as_of=packet.as_of,
        stance=stance,
        confidence=conf,
        supporting_evidence=support,
        contradictory_evidence=contra,
        risks=risks,
        data_quality_issues=quality,
        candidate_assessment=assessment,
        recommendation=recommendation,
        reasoning_summary=summary,
        created_at=packet.as_of,
        report_schema_version=REPORT_SCHEMA,
    )


def _candidate(packet: ResearchPacket) -> dict:
    return dict(packet.option_candidate or {})


class BullAgent:
    agent_id = "bull"
    agent_version = AGENT_VERSION

    def __init__(self, prompt_version: str = "v1") -> None:
        self.prompt_version = prompt_version

    def research(self, packet: ResearchPacket) -> ResearchReport:
        if not packet.strategy_evidence or not packet.market_research_view:
            return _report(
                packet,
                agent_id=self.agent_id,
                prompt_version=self.prompt_version,
                stance=Stance.INSUFFICIENT_DATA,
                recommendation=Recommendation.ABSTAIN,
                confidence=0.0,
                support=(),
                contra=("missing strategy or market view",),
                assessment="insufficient",
                summary="Bull desk: required evidence missing.",
            )
        cand = _candidate(packet)
        direction = packet.strategy_direction
        support = (
            f"strategy_direction={direction}",
            f"strategy_confidence={packet.strategy_evidence.get('confidence')}",
            f"signal_id={packet.strategy_signal_id}",
        )
        contra: list[str] = []
        if packet.options_status != "CANDIDATE" or not cand:
            contra.append("no_option_candidate")
            rec = Recommendation.ABSTAIN
            stance = Stance.BULLISH if direction == "BULLISH" else Stance.NEUTRAL
        elif cand.get("option_type") == "CE" and direction == "BULLISH":
            rec = Recommendation.SUPPORT
            stance = Stance.BULLISH
            support += (f"candidate={cand['candidate_id']} BUY CE",)
        else:
            rec = Recommendation.OPPOSE
            stance = Stance.BULLISH
            contra.append(f"candidate_type={cand.get('option_type')} is not BUY CE")
        return _report(
            packet,
            agent_id=self.agent_id,
            prompt_version=self.prompt_version,
            stance=stance,
            recommendation=rec,
            confidence=0.55 if rec is Recommendation.SUPPORT else 0.35,
            support=support,
            contra=tuple(contra),
            assessment=str(cand.get("candidate_id") or "none"),
            summary=f"Bull desk {rec.value} on packet {packet.packet_id}.",
        )


class BearAgent:
    agent_id = "bear"
    agent_version = AGENT_VERSION

    def __init__(self, prompt_version: str = "v1") -> None:
        self.prompt_version = prompt_version

    def research(self, packet: ResearchPacket) -> ResearchReport:
        if not packet.strategy_evidence or not packet.market_research_view:
            return _report(
                packet,
                agent_id=self.agent_id,
                prompt_version=self.prompt_version,
                stance=Stance.INSUFFICIENT_DATA,
                recommendation=Recommendation.ABSTAIN,
                confidence=0.0,
                support=(),
                contra=("missing strategy or market view",),
                assessment="insufficient",
                summary="Bear desk: required evidence missing.",
            )
        cand = _candidate(packet)
        direction = packet.strategy_direction
        support = (
            f"strategy_direction={direction}",
            f"strategy_confidence={packet.strategy_evidence.get('confidence')}",
            f"signal_id={packet.strategy_signal_id}",
        )
        contra: list[str] = []
        if packet.options_status != "CANDIDATE" or not cand:
            contra.append("no_option_candidate")
            rec = Recommendation.ABSTAIN
            stance = Stance.BEARISH if direction == "BEARISH" else Stance.NEUTRAL
        elif cand.get("option_type") == "PE" and direction == "BEARISH":
            rec = Recommendation.SUPPORT
            stance = Stance.BEARISH
            support += (f"candidate={cand['candidate_id']} BUY PE",)
        else:
            rec = Recommendation.OPPOSE
            stance = Stance.BEARISH
            contra.append(f"candidate_type={cand.get('option_type')} is not BUY PE")
        return _report(
            packet,
            agent_id=self.agent_id,
            prompt_version=self.prompt_version,
            stance=stance,
            recommendation=rec,
            confidence=0.55 if rec is Recommendation.SUPPORT else 0.35,
            support=support,
            contra=tuple(contra),
            assessment=str(cand.get("candidate_id") or "none"),
            summary=f"Bear desk {rec.value} on packet {packet.packet_id}.",
        )


class QuantAgent:
    agent_id = "quant"
    agent_version = AGENT_VERSION

    def __init__(self, prompt_version: str = "v1") -> None:
        self.prompt_version = prompt_version

    def research(self, packet: ResearchPacket) -> ResearchReport:
        if not packet.strategy_evidence:
            return _report(
                packet,
                agent_id=self.agent_id,
                prompt_version=self.prompt_version,
                stance=Stance.INSUFFICIENT_DATA,
                recommendation=Recommendation.ABSTAIN,
                confidence=0.0,
                support=(),
                contra=("missing strategy_evidence",),
                assessment="insufficient",
                summary="Quant: no strategy evidence.",
            )
        cand = _candidate(packet)
        direction = packet.strategy_direction
        conf = float(packet.strategy_evidence.get("confidence") or 0)
        support = (
            f"strategy={packet.strategy_evidence.get('strategy')}",
            f"strategy_confidence={conf}",
            f"regime={packet.strategy_evidence.get('regime')}",
        )
        contra: list[str] = []
        risks: list[str] = []
        if packet.options_status != "CANDIDATE" or not cand:
            rec = Recommendation.OPPOSE
            stance = Stance.NEUTRAL
            contra.append("options_status is not CANDIDATE")
        else:
            mapped = "CE" if direction == "BULLISH" else "PE" if direction == "BEARISH" else ""
            if cand.get("option_type") != mapped:
                rec = Recommendation.OPPOSE
                stance = Stance.NEUTRAL
                contra.append("direction_type_mismatch")
            else:
                rec = Recommendation.SUPPORT
                stance = Stance.BULLISH if direction == "BULLISH" else Stance.BEARISH
                support += (f"score_total={cand.get('score_total')}", f"moneyness={cand.get('moneyness')}")
                if (cand.get("score_total") or 0) < 0.2:
                    risks.append("low_option_score")
        return _report(
            packet,
            agent_id=self.agent_id,
            prompt_version=self.prompt_version,
            stance=stance,
            recommendation=rec,
            confidence=min(0.7, max(0.2, conf)),
            support=support,
            contra=tuple(contra),
            risks=tuple(risks),
            assessment=str(cand.get("candidate_id") or "none"),
            summary=f"Quant {rec.value}; strategy_confidence={conf}.",
        )


class RiskContextAgent:
    agent_id = "risk_context"
    agent_version = AGENT_VERSION

    def __init__(self, prompt_version: str = "v1") -> None:
        self.prompt_version = prompt_version

    def research(self, packet: ResearchPacket) -> ResearchReport:
        if not packet.session_context:
            return _report(
                packet,
                agent_id=self.agent_id,
                prompt_version=self.prompt_version,
                stance=Stance.INSUFFICIENT_DATA,
                recommendation=Recommendation.ABSTAIN,
                confidence=0.0,
                support=(),
                contra=("missing session_context",),
                assessment="insufficient",
                summary="Risk context: no session data.",
            )
        cand = _candidate(packet)
        quality: list[str] = []
        risks: list[str] = []
        support = (
            f"session={packet.session_context.get('session')}",
            f"quality_stale={packet.session_context.get('quality_stale')}",
        )
        if packet.session_context.get("quality_stale"):
            quality.append("stale_market_view")
        if packet.session_context.get("quality_complete") is False:
            quality.append("incomplete_bars")
        if packet.options_status != "CANDIDATE" or not cand:
            rec = Recommendation.OPPOSE
            stance = Stance.INSUFFICIENT_DATA if not cand else Stance.NEUTRAL
            risks.append("no_candidate")
        elif packet.session_context.get("quality_stale"):
            rec = Recommendation.OPPOSE
            stance = Stance.NEUTRAL
        else:
            spread = cand.get("spread_pct")
            if spread is not None and spread > 0.15:
                rec = Recommendation.OPPOSE
                stance = Stance.NEUTRAL
                risks.append("wide_spread")
            else:
                rec = Recommendation.SUPPORT
                stance = Stance.NEUTRAL
                support += (f"spread_pct={spread}", f"open_interest={cand.get('open_interest')}")
        return _report(
            packet,
            agent_id=self.agent_id,
            prompt_version=self.prompt_version,
            stance=stance,
            recommendation=rec,
            confidence=0.5,
            support=support,
            contra=(),
            risks=tuple(risks),
            quality=tuple(quality),
            assessment=str(cand.get("candidate_id") or "none"),
            summary=f"Risk-context {rec.value} (research, not Risk Guard).",
        )
