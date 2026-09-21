"""2D research/CEO contracts. Advisory only. Not orders."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


PACKET_SCHEMA = "research.packet.v1"
REPORT_SCHEMA = "research.report.v1"
DECISION_SCHEMA = "research.decision.v1"


class Stance(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Recommendation(str, Enum):
    SUPPORT = "SUPPORT"
    OPPOSE = "OPPOSE"
    ABSTAIN = "ABSTAIN"


class CEOVerdict(str, Enum):
    TRADE_APPROVE = "TRADE_APPROVE"
    NO_TRADE = "NO_TRADE"


def _digest(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ResearchPacket:
    packet_id: str
    as_of: datetime
    underlying: str
    strategy_signal_id: str
    strategy_signal_version: str
    options_decision_id: str
    option_candidate_id: str | None
    market_research_view: Mapping[str, Any]
    strategy_evidence: Mapping[str, Any]
    option_candidate: Mapping[str, Any] | None
    rejected_candidate_summary: tuple[str, ...]
    session_context: Mapping[str, Any]
    configuration_version: str
    data_snapshot_ids: Mapping[str, str]
    packet_schema_version: str
    options_status: str
    strategy_direction: str
    evidence_for_agents: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "as_of": self.as_of.isoformat(),
            "underlying": self.underlying,
            "strategy_signal_id": self.strategy_signal_id,
            "strategy_signal_version": self.strategy_signal_version,
            "options_decision_id": self.options_decision_id,
            "option_candidate_id": self.option_candidate_id,
            "market_research_view": dict(self.market_research_view),
            "strategy_evidence": dict(self.strategy_evidence),
            "option_candidate": None if self.option_candidate is None else dict(self.option_candidate),
            "rejected_candidate_summary": list(self.rejected_candidate_summary),
            "session_context": dict(self.session_context),
            "configuration_version": self.configuration_version,
            "data_snapshot_ids": dict(self.data_snapshot_ids),
            "packet_schema_version": self.packet_schema_version,
            "options_status": self.options_status,
            "strategy_direction": self.strategy_direction,
            "evidence_for_agents": list(self.evidence_for_agents),
        }


@dataclass(frozen=True)
class ResearchReport:
    report_id: str
    agent_id: str
    agent_version: str
    prompt_version: str
    model_provider: str
    model_name: str
    packet_id: str
    as_of: datetime
    stance: Stance
    confidence: float
    supporting_evidence: tuple[str, ...]
    contradictory_evidence: tuple[str, ...]
    risks: tuple[str, ...]
    data_quality_issues: tuple[str, ...]
    candidate_assessment: str
    recommendation: Recommendation
    reasoning_summary: str
    created_at: datetime
    report_schema_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "packet_id": self.packet_id,
            "as_of": self.as_of.isoformat(),
            "stance": self.stance.value,
            "confidence": self.confidence,
            "supporting_evidence": list(self.supporting_evidence),
            "contradictory_evidence": list(self.contradictory_evidence),
            "risks": list(self.risks),
            "data_quality_issues": list(self.data_quality_issues),
            "candidate_assessment": self.candidate_assessment,
            "recommendation": self.recommendation.value,
            "reasoning_summary": self.reasoning_summary,
            "created_at": self.created_at.isoformat(),
            "report_schema_version": self.report_schema_version,
            "executed": False,
        }


@dataclass(frozen=True)
class CEODecision:
    decision_id: str
    packet_id: str
    decision: CEOVerdict
    selected_candidate_id: str | None
    direction: str
    rationale_summary: str
    supporting_report_ids: tuple[str, ...]
    conflicting_report_ids: tuple[str, ...]
    confidence: float
    uncertainty_flags: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    approval_basis: tuple[str, ...]
    model_provider: str
    model_name: str
    prompt_version: str
    decision_schema_version: str
    as_of: datetime
    created_at: datetime
    validation_ok: bool
    validation_failures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "packet_id": self.packet_id,
            "decision": self.decision.value,
            "selected_candidate_id": self.selected_candidate_id,
            "direction": self.direction,
            "rationale_summary": self.rationale_summary,
            "supporting_report_ids": list(self.supporting_report_ids),
            "conflicting_report_ids": list(self.conflicting_report_ids),
            "confidence": self.confidence,
            "uncertainty_flags": list(self.uncertainty_flags),
            "rejection_reasons": list(self.rejection_reasons),
            "approval_basis": list(self.approval_basis),
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "decision_schema_version": self.decision_schema_version,
            "as_of": self.as_of.isoformat(),
            "created_at": self.created_at.isoformat(),
            "validation_ok": self.validation_ok,
            "validation_failures": list(self.validation_failures),
            "executed": False,
        }


@dataclass(frozen=True)
class AuditRecord:
    packet_id: str
    report_ids: tuple[str, ...]
    decision_id: str
    decision: str
    candidate_id: str | None
    snapshot_ids: Mapping[str, str]
    prompt_versions: Mapping[str, str]
    model_provider: str
    validation_ok: bool
    validation_failures: tuple[str, ...]
    as_of: datetime
    schema_versions: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "report_ids": list(self.report_ids),
            "decision_id": self.decision_id,
            "decision": self.decision,
            "candidate_id": self.candidate_id,
            "snapshot_ids": dict(self.snapshot_ids),
            "prompt_versions": dict(self.prompt_versions),
            "model_provider": self.model_provider,
            "validation_ok": self.validation_ok,
            "validation_failures": list(self.validation_failures),
            "as_of": self.as_of.isoformat(),
            "schema_versions": dict(self.schema_versions),
        }


def packet_id_for(parts: Mapping[str, Any]) -> str:
    return _digest({"kind": "packet", **parts})


def report_id_for(*, packet_id: str, agent_id: str, prompt_version: str, stance: str, recommendation: str) -> str:
    return _digest(
        {
            "agent_id": agent_id,
            "packet_id": packet_id,
            "prompt_version": prompt_version,
            "recommendation": recommendation,
            "stance": stance,
        }
    )


def decision_id_for(*, packet_id: str, decision: str, candidate_id: str | None, prompt_version: str) -> str:
    return _digest(
        {
            "candidate_id": candidate_id or "",
            "decision": decision,
            "packet_id": packet_id,
            "prompt_version": prompt_version,
        }
    )
