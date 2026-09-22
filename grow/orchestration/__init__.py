"""Requirement 4B orchestration layer — analysis only, no broker orders.

Canonical pipeline: ``AnalysisOrchestrator`` (dispatch → validate → aggregate).
``AgentCycleOrchestrator`` remains a compatibility Risk Guard consumer only.
"""

from grow.orchestration.aggregator import aggregate_outputs
from grow.orchestration.cycle import AnalysisCycleStore, AnalysisOrchestrator, stable_cycle_id
from grow.orchestration.dispatcher import dispatch_agents
from grow.orchestration.models import AggregateAnalysisPackage, AgentDispatchRecord
from grow.orchestration.validator import ValidationOutcome, validate_agent_output

__all__ = [
    "AggregateAnalysisPackage",
    "AgentDispatchRecord",
    "AnalysisCycleStore",
    "AnalysisOrchestrator",
    "ValidationOutcome",
    "aggregate_outputs",
    "dispatch_agents",
    "stable_cycle_id",
    "validate_agent_output",
]
