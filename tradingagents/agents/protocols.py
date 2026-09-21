"""Agent role protocols.

These names match a typical trading-desk decomposition (analysts, bull/bear
researchers, trader). Implementations in milestone 1 are deterministic
functions, not LLM loops.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AgentNote:
    role: str
    headline: str
    detail: str
    bias: str  # bull / bear / neutral


class Analyst(Protocol):
    role: str

    def run(self, ticker: str, context: dict) -> AgentNote: ...


class Researcher(Protocol):
    stance: str

    def run(self, ticker: str, notes: list[AgentNote]) -> AgentNote: ...


class Trader(Protocol):
    def plan(self, ticker: str, notes: list[AgentNote]) -> AgentNote: ...
