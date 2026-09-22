"""Phase-4 Decision Engine — deterministic BUY_CE / BUY_PE / NO_TRADE surface.

Wraps ``DecisionIntegrator`` so Risk Guard remains the final safety authority.
Does not submit paper fills and does not call a broker.
"""

from __future__ import annotations

from grow.config import GrowConfig
from grow.decision.integration.contract import (
    DecisionAction,
    DecisionBookState,
    IntegratedDecision,
    TradeCandidate,
)
from grow.decision.integration.integrator import DecisionAuditLog, DecisionIntegrator
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
    ) -> None:
        self._integrator = DecisionIntegrator(
            config,
            risk_guard=risk_guard,
            audit=audit,
            risk_secret=risk_secret,
        )

    @property
    def audit(self) -> DecisionAuditLog:
        return self._integrator.audit

    @property
    def integrator(self) -> DecisionIntegrator:
        """Compatibility access to the underlying 4C integrator."""
        return self._integrator

    def decide(
        self,
        *,
        snapshot: AgentMarketSnapshot,
        package: AggregateAnalysisPackage,
        book: DecisionBookState | None = None,
    ) -> IntegratedDecision:
        """Produce one auditable decision with explicit DecisionAction."""
        return self._integrator.integrate(snapshot=snapshot, package=package, book=book)

    def replay(self, decision_id: str) -> IntegratedDecision:
        return self._integrator.replay(decision_id)

    @staticmethod
    def action_of(decision: IntegratedDecision) -> DecisionAction:
        return decision.action

    @staticmethod
    def trade_candidate_of(decision: IntegratedDecision) -> TradeCandidate | None:
        return decision.trade_candidate
