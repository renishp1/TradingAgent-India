"""Structured debate / agreement summary. Does not average opinions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction


@dataclass(frozen=True)
class DebateSummary:
    agreement: bool
    actions: tuple[str, ...]
    agreeing_agents: tuple[str, ...]
    dissenting_agents: tuple[str, ...]
    conflicts: tuple[str, ...]
    evidence: tuple[str, ...]
    insufficient_agents: tuple[str, ...]
    error_agents: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agreement": self.agreement,
            "actions": list(self.actions),
            "agreeing_agents": list(self.agreeing_agents),
            "dissenting_agents": list(self.dissenting_agents),
            "conflicts": list(self.conflicts),
            "evidence": list(self.evidence),
            "insufficient_agents": list(self.insufficient_agents),
            "error_agents": list(self.error_agents),
        }


def summarize_debate(results: tuple[AgentResult, ...]) -> DebateSummary:
    """Record every response and identify conflicting recommendations."""
    if not results:
        return DebateSummary(
            agreement=False,
            actions=(),
            agreeing_agents=(),
            dissenting_agents=(),
            conflicts=("NO_AGENT_RESULTS",),
            evidence=(),
            insufficient_agents=(),
            error_agents=(),
        )

    actionable = [
        row
        for row in results
        if row.status is AgentStatus.PASS
        and row.candidate_action
        not in {CandidateAction.NONE, CandidateAction.ABSTAIN, CandidateAction.HOLD}
    ]
    actions = tuple(sorted({row.candidate_action.value for row in actionable}))
    instruments = tuple(
        sorted({row.candidate_instrument or "" for row in actionable if row.candidate_instrument})
    )

    conflicts: list[str] = []
    evidence: list[str] = []
    if len(actions) > 1:
        conflicts.append(f"ACTION_CONFLICT:{','.join(actions)}")
        for row in actionable:
            evidence.append(
                f"{row.agent_name}:{row.candidate_action.value}:{row.entry_reason or row.status.value}"
            )
    if len(instruments) > 1:
        conflicts.append(f"INSTRUMENT_CONFLICT:{','.join(instruments)}")
        for row in actionable:
            if row.candidate_instrument:
                evidence.append(f"{row.agent_name}:instrument:{row.candidate_instrument}")

    majority_action = actions[0] if len(actions) == 1 else None
    agreeing = tuple(
        row.agent_name
        for row in actionable
        if majority_action is not None and row.candidate_action.value == majority_action
    )
    dissenting = tuple(
        row.agent_name
        for row in results
        if row.agent_name not in agreeing
        and row.status not in {AgentStatus.DATA_INSUFFICIENT, AgentStatus.ERROR}
    )
    insufficient = tuple(
        row.agent_name for row in results if row.status is AgentStatus.DATA_INSUFFICIENT
    )
    errors = tuple(row.agent_name for row in results if row.status is AgentStatus.ERROR)

    agreement = bool(majority_action) and not conflicts and not insufficient and not errors
    return DebateSummary(
        agreement=agreement,
        actions=actions,
        agreeing_agents=agreeing,
        dissenting_agents=dissenting,
        conflicts=tuple(conflicts),
        evidence=tuple(evidence),
        insufficient_agents=insufficient,
        error_agents=errors,
    )
