"""Requirement 4C — decision integration, audit, and safety.

Consumes a validated 4B ``AggregateAnalysisPackage``. Risk Guard remains the
final safety authority. No broker order path.
"""

from grow.decision.integration.contract import (
    DECISION_SCHEMA,
    REQUIRED_DECISION_FIELDS,
    AgentOutputRef,
    DecisionBookState,
    IntegratedDecision,
    IntegratedDecisionStatus,
    StrategyCandidate,
)
from grow.decision.integration.integrator import DecisionAuditLog, DecisionIntegrator
from grow.decision.integration.policy import classify_output

__all__ = [
    "DECISION_SCHEMA",
    "REQUIRED_DECISION_FIELDS",
    "AgentOutputRef",
    "DecisionAuditLog",
    "DecisionBookState",
    "DecisionIntegrator",
    "IntegratedDecision",
    "IntegratedDecisionStatus",
    "StrategyCandidate",
    "classify_output",
]
