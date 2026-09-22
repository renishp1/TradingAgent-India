"""Requirement 4C — decision integration, audit, and safety.

Consumes a validated 4B ``AggregateAnalysisPackage``. Risk Guard remains the
final safety authority. No broker order path.

Phase 4 exposes ``DecisionEngine`` with explicit BUY_CE / BUY_PE / NO_TRADE.
"""

from grow.decision.integration.contract import (
    DECISION_SCHEMA,
    REQUIRED_DECISION_FIELDS,
    AgentOutputRef,
    DecisionAction,
    DecisionBookState,
    IntegratedDecision,
    IntegratedDecisionStatus,
    StrategyCandidate,
    TradeCandidate,
    build_trade_candidate,
    resolve_decision_action,
)
from grow.decision.integration.engine import DecisionEngine
from grow.decision.integration.integrator import DecisionAuditLog, DecisionIntegrator
from grow.decision.integration.policy import classify_output

__all__ = [
    "DECISION_SCHEMA",
    "REQUIRED_DECISION_FIELDS",
    "AgentOutputRef",
    "DecisionAction",
    "DecisionAuditLog",
    "DecisionBookState",
    "DecisionEngine",
    "DecisionIntegrator",
    "IntegratedDecision",
    "IntegratedDecisionStatus",
    "StrategyCandidate",
    "TradeCandidate",
    "build_trade_candidate",
    "classify_output",
    "resolve_decision_action",
]
