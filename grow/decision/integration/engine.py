"""Phase-4 Decision Engine — deterministic BUY_CE / BUY_PE / NO_TRADE surface.

Wraps ``DecisionIntegrator`` so Risk Guard remains the final safety authority.
Phase 7 attaches a dedicated ``SignalEngine`` explanation (supporting +
counter-evidence) without changing Risk Guard authority.
Does not submit paper fills and does not call a broker.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from grow.config import GrowConfig
from grow.decision.integration.contract import (
    DecisionAction,
    DecisionBookState,
    IntegratedDecision,
    TradeCandidate,
)
from grow.decision.integration.integrator import DecisionAuditLog, DecisionIntegrator
from grow.decision.signal.engine import SignalEngine
from grow.decision.signal.models import CampaignSignal
from grow.market_data.normalized.models import AgentMarketSnapshot
from grow.orchestration.models import AggregateAnalysisPackage
from grow.risk.guard import RiskGuard


class DecisionEngine:
    """Named Phase-4 decision surface over the existing 4C integrator."""

    def __init__(
        self,
        config: GrowConfig,
        *,
        risk_guard: RiskGuard | None = None,
        audit: DecisionAuditLog | None = None,
        risk_secret: str | None = None,
        signal_engine: SignalEngine | None = None,
    ) -> None:
        self._integrator = DecisionIntegrator(
            config,
            risk_guard=risk_guard,
            audit=audit,
            risk_secret=risk_secret,
        )
        self._signal_engine = signal_engine or SignalEngine(config)

    @property
    def audit(self) -> DecisionAuditLog:
        return self._integrator.audit

    @property
    def integrator(self) -> DecisionIntegrator:
        """Compatibility access to the underlying 4C integrator."""
        return self._integrator

    @property
    def signal_engine(self) -> SignalEngine:
        return self._signal_engine

    def decide(
        self,
        *,
        snapshot: AgentMarketSnapshot,
        package: AggregateAnalysisPackage,
        book: DecisionBookState | None = None,
    ) -> IntegratedDecision:
        """Produce one auditable decision with explicit DecisionAction + campaign signal."""
        signal = self._signal_engine.evaluate(snapshot=snapshot, package=package)
        decision = self._integrator.integrate(snapshot=snapshot, package=package, book=book)
        return self._with_signal(decision, signal)

    def explain(
        self,
        *,
        snapshot: AgentMarketSnapshot,
        package: AggregateAnalysisPackage,
    ) -> CampaignSignal:
        """Signal-only evaluation (no Risk Guard). Useful for audit/tests."""
        return self._signal_engine.evaluate(snapshot=snapshot, package=package)

    def replay(self, decision_id: str) -> IntegratedDecision:
        """Replay integrator decision and re-attach deterministic campaign signal."""
        record = self.audit.get(decision_id)
        decision = self._integrator.replay(decision_id)
        signal = self._signal_engine.evaluate(snapshot=record.snapshot, package=record.package)
        return self._with_signal(decision, signal)

    def _with_signal(self, decision: IntegratedDecision, signal: CampaignSignal) -> IntegratedDecision:
        evidence = dict(decision.calculated_evidence)
        evidence["campaign_signal"] = signal.to_dict()
        evidence["signal_engine_version"] = signal.engine_version
        return replace(
            decision,
            campaign_signal=signal.to_dict(),
            calculated_evidence=evidence,
        )

    @staticmethod
    def action_of(decision: IntegratedDecision) -> DecisionAction:
        return decision.action

    @staticmethod
    def trade_candidate_of(decision: IntegratedDecision) -> TradeCandidate | None:
        return decision.trade_candidate

    @staticmethod
    def signal_of(decision: IntegratedDecision) -> Mapping[str, Any] | None:
        return decision.campaign_signal
