"""Multi-agent research interfaces. Paper analysis only — no broker orders."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from grow.agents.base import SpecialistAgent

if TYPE_CHECKING:
    from grow.agents.orchestrator.cycle import AgentCycleOrchestrator, OrchestratorDecision

__all__ = [
    "AgentCycleOrchestrator",
    "OrchestratorDecision",
    "SpecialistAgent",
]


def __getattr__(name: str) -> Any:
    # Lazy export avoids circular import with grow.orchestration.cycle.
    if name in {"AgentCycleOrchestrator", "OrchestratorDecision"}:
        from grow.agents.orchestrator.cycle import AgentCycleOrchestrator, OrchestratorDecision

        return AgentCycleOrchestrator if name == "AgentCycleOrchestrator" else OrchestratorDecision
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
