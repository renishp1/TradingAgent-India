"""Requirement 4C — decision integration, audit, and safety.

Consumes a validated 4B ``AggregateAnalysisPackage``. Risk Guard remains the
final safety authority. No broker order path.

Phase 4 exposes ``DecisionEngine`` with explicit BUY_CE / BUY_PE / NO_TRADE.
Phase 6 adds campaign option-chain intelligence as the sole candidate filter.
Phase 7 attaches a dedicated Signal Engine (explained action + counter-evidence).
"""

from grow.decision.integration.chain_filter import (
    CHAIN_FILTER_REJECTED,
    CHAIN_FILTER_VERSION,
    CHAIN_UNIVERSE_EMPTY,
    MISSING_OPTION_CHAIN,
    ChainFilterResult,
    allow_campaign_candidate,
    filter_campaign_chain,
)
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
from grow.decision.signal import (
    SIGNAL_ENGINE_VERSION,
    SIGNAL_SCHEMA,
    CampaignSignal,
    CounterEvidence,
    SignalEngine,
)

__all__ = [
    "CHAIN_FILTER_REJECTED",
    "CHAIN_FILTER_VERSION",
    "CHAIN_UNIVERSE_EMPTY",
    "DECISION_SCHEMA",
    "MISSING_OPTION_CHAIN",
    "REQUIRED_DECISION_FIELDS",
    "SIGNAL_ENGINE_VERSION",
    "SIGNAL_SCHEMA",
    "AgentOutputRef",
    "CampaignSignal",
    "ChainFilterResult",
    "CounterEvidence",
    "DecisionAction",
    "DecisionAuditLog",
    "DecisionBookState",
    "DecisionEngine",
    "DecisionIntegrator",
    "IntegratedDecision",
    "IntegratedDecisionStatus",
    "SignalEngine",
    "StrategyCandidate",
    "TradeCandidate",
    "allow_campaign_candidate",
    "build_trade_candidate",
    "classify_output",
    "filter_campaign_chain",
    "resolve_decision_action",
]
