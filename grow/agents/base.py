"""Specialist agent protocol shared by all read-only analysts."""

from __future__ import annotations

from typing import Protocol

from grow.decision.contracts.agent_result import AgentResult
from grow.market_data.normalized.models import AgentMarketSnapshot


class SpecialistAgent(Protocol):
    agent_name: str
    agent_version: str

    def analyze(self, snapshot: AgentMarketSnapshot) -> AgentResult:
        """Return a structured result for the shared snapshot. Never places orders."""
        ...
