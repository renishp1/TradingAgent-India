"""Phase-4/7 Decision Engine — BUY_CE / BUY_PE / NO_TRADE with campaign signal.

Wraps ``DecisionIntegrator`` so Risk Guard remains the final safety authority.
Policy selection is followed by a deterministic campaign CEO gate (bull→CE /
bear→PE), then Risk Guard. Phase 7 evaluates ``SignalEngine`` for explained
supporting/counter-evidence, then attaches the ``CampaignSignal`` to the
integrator decision.

Order of authority:
1. DecisionIntegrator policy + campaign CEO gate + Risk Guard produce the
   authoritative decision.
2. SignalEngine explains intent; it is never an execution authority.
3. No paper fills and no broker order path live here.
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
    """Named decision surface: integrator (Risk Guard) + SignalEngine explanation."""

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
        """Evaluate signal → integrate once (Risk Guard) → attach campaign_signal."""
        signal = self._signal_engine.evaluate(
            snapshot=snapshot,
            package=package,
        )
        decision = self._integrator.integrate(
            snapshot=snapshot,
            package=package,
            book=book,
        )
        return self._with_signal(decision, signal)

    def explain(
        self,
        *,
        snapshot: AgentMarketSnapshot,
        package: AggregateAnalysisPackage,
    ) -> CampaignSignal:
        """Signal-only evaluation. Does not invoke Risk Guard or paper fills."""
        return self._signal_engine.evaluate(snapshot=snapshot, package=package)

    def replay(self, decision_id: str) -> IntegratedDecision:
        """Replay integrator once, re-evaluate signal from audit record, attach."""
        record = self.audit.get(decision_id)
        decision = self._integrator.replay(decision_id)
        signal = self._signal_engine.evaluate(
            snapshot=record.snapshot,
            package=record.package,
        )
        return self._with_signal(decision, signal)

    def _with_signal(self, decision: IntegratedDecision, signal: CampaignSignal) -> IntegratedDecision:
        """Attach CampaignSignal explanation. Does not alter Risk Guard outcome."""
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
