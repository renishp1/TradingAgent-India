"""DecisionRecord — audit backlog, not in the execution HMAC.

Thesis, confidence, model version, and research inputs are commentary.
They do not belong in RiskStamp. Persist them here in a later milestone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class DecisionRecord:
    proposal: Mapping[str, Any]
    model: str | None
    model_version: str | None
    prompt_hash: str | None
    research_inputs: Mapping[str, Any]
    strategy_signals: tuple[Mapping[str, Any], ...]
    confidence: float | None
    thesis: str
    timestamp: datetime
    risk_verdict: Mapping[str, Any]
    risk_rules: tuple[tuple[str, bool, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal": dict(self.proposal),
            "model": self.model,
            "model_version": self.model_version,
            "prompt_hash": self.prompt_hash,
            "research_inputs": dict(self.research_inputs),
            "strategy_signals": list(self.strategy_signals),
            "confidence": self.confidence,
            "thesis": self.thesis,
            "timestamp": self.timestamp.isoformat(),
            "risk_verdict": dict(self.risk_verdict),
            "risk_rules": [
                {"id": rid, "passed": passed, "detail": detail}
                for rid, passed, detail in self.risk_rules
            ],
        }
