"""ResearchOrchestrator: packet → reports → CEO → validate. No execution."""

from __future__ import annotations

from grow.config import GrowConfig, load_config
from grow.data.boundary import assert_research_payload
from grow.errors import GrowConfigError
from grow.research.agents import BearAgent, BullAgent, QuantAgent, ResearchAgent, RiskContextAgent
from grow.research.audit import AuditLog
from grow.research.ceo_agent import CEOAgent
from grow.research.models import CEODecision, ResearchPacket, ResearchReport
from grow.research.validate import DecisionValidator, asof_failures, no_trade


class ResearchOrchestrator:
    def __init__(self, config: GrowConfig | None = None, *, audit: AuditLog | None = None) -> None:
        self.config = config or load_config()
        ai = self.config.ai
        if ai.allow_broker or ai.allow_live_trading or ai.allow_ai_execution:
            raise GrowConfigError("2D AI execution/broker flags must be false.")
        if ai.provider != "fixture":
            raise GrowConfigError("2D ai.provider must be fixture.")
        self.audit = audit or AuditLog()
        self.validator = DecisionValidator(ai)
        versions = ai.prompt_versions
        self.agents: tuple[ResearchAgent, ...] = (
            BullAgent(versions.get("bull", "v1")),
            BearAgent(versions.get("bear", "v1")),
            QuantAgent(versions.get("quant", "v1")),
            RiskContextAgent(versions.get("risk_context", "v1")),
        )
        self.ceo = CEOAgent(versions.get("ceo", "v1"))

    def run(self, packet: ResearchPacket) -> CEODecision:
        prompts = {
            "bull": self.config.ai.prompt_versions.get("bull", "v1"),
            "bear": self.config.ai.prompt_versions.get("bear", "v1"),
            "quant": self.config.ai.prompt_versions.get("quant", "v1"),
            "risk_context": self.config.ai.prompt_versions.get("risk_context", "v1"),
            "ceo": self.config.ai.prompt_versions.get("ceo", "v1"),
        }
        try:
            assert_research_payload(packet.to_dict())
        except Exception as exc:
            decision = no_trade(packet, reasons=(f"PACKET_INVALID:{exc}",), prompt_version=prompts["ceo"])
            self.audit.append(packet, (), decision, prompts)
            return decision
        if not self.config.ai.enabled:
            decision = no_trade(packet, reasons=("AI_DISABLED",), prompt_version=prompts["ceo"])
            self.audit.append(packet, (), decision, prompts)
            return decision
        mismatch = asof_failures(packet)
        if mismatch:
            decision = no_trade(packet, reasons=mismatch, prompt_version=prompts["ceo"])
            self.audit.append(packet, (), decision, prompts)
            return decision
        reports: list[ResearchReport] = []
        for agent in self.agents:
            try:
                report = agent.research(packet)
            except TimeoutError:
                decision = no_trade(packet, reasons=(f"PROVIDER_TIMEOUT:{agent.agent_id}",), reports=tuple(reports))
                self.audit.append(packet, tuple(reports), decision, prompts)
                return decision
            except Exception as exc:
                decision = no_trade(
                    packet,
                    reasons=(f"PROVIDER_FAILURE:{agent.agent_id}:{type(exc).__name__}",),
                    reports=tuple(reports),
                )
                self.audit.append(packet, tuple(reports), decision, prompts)
                return decision
            try:
                _validate_report(report, packet)
            except ValueError as exc:
                decision = no_trade(
                    packet,
                    reasons=(f"MALFORMED_REPORT:{agent.agent_id}:{exc}",),
                    reports=tuple(reports),
                )
                self.audit.append(packet, tuple(reports), decision, prompts)
                return decision
            reports.append(report)
        bundled = tuple(reports)
        raw_decision = self.ceo.synthesize(packet, bundled)
        validated = self.validator.validate(packet, raw_decision, bundled)
        self.audit.append(packet, bundled, validated, prompts)
        return validated


def _validate_report(report: ResearchReport, packet: ResearchPacket) -> None:
    if report.packet_id != packet.packet_id:
        raise ValueError("packet_id")
    if report.report_schema_version != "research.report.v1":
        raise ValueError("schema")
    if not (0.0 <= report.confidence <= 1.0):
        raise ValueError("confidence")
    if report.stance.value in {"SELL", "SHORT", "BUY"}:
        raise ValueError("execution_language")
