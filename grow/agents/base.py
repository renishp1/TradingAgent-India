"""Specialist agent protocol shared by all read-only analysts."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from grow.decision.contracts.agent_result import AgentInput, AgentResult
from grow.market_data.normalized.models import AgentMarketSnapshot


@runtime_checkable
class SpecialistAgent(Protocol):
    agent_name: str
    agent_version: str

    def analyze(
        self,
        snapshot: AgentMarketSnapshot,
        *,
        cycle_id: str = "",
    ) -> AgentResult:
        """Return a structured result for the shared snapshot. Never places orders."""
        ...

    def analyze_input(self, agent_input: AgentInput) -> AgentResult:
        """Preferred 4B entrypoint using the common input contract."""
        ...
