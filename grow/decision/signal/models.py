"""Phase 7 — campaign Signal Engine contracts.

Deterministic explained BUY_CE / BUY_PE / NO_TRADE with counter-evidence.
Not an order. Not a Risk Guard stamp. Not a broker path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from grow.decision.integration.contract import DecisionAction, digest_payload, plain_data


SIGNAL_SCHEMA = "grow.decision.signal.v1"
SIGNAL_ENGINE_VERSION = "campaign.signal_engine.v1"


@dataclass(frozen=True)
class CounterEvidence:
    """One structured reason that argues against taking the campaign trade."""

    code: str
    source: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "source": self.source, "detail": self.detail}


@dataclass(frozen=True)
class CampaignSignal:
    """Explained campaign signal. Buyer-only actions only."""

    signal_id: str
    action: DecisionAction
    direction: str | None
    underlying: str | None
    instrument: str | None
    option_type: str | None
    explanation: str
    supporting_evidence: tuple[str, ...]
    counter_evidence: tuple[CounterEvidence, ...]
    reason_codes: tuple[str, ...]
    package_digest: str
    snapshot_id: str
    snapshot_version: str
    analysis_cycle_id: str
    engine_version: str = SIGNAL_ENGINE_VERSION
    schema_version: str = SIGNAL_SCHEMA

    def __post_init__(self) -> None:
        if self.action not in set(DecisionAction):
            raise ValueError("unsupported signal action")
        if self.action in {DecisionAction.BUY_CE, DecisionAction.BUY_PE}:
            if self.option_type not in {"CE", "PE"}:
                raise ValueError("BUY_CE/BUY_PE require option_type")
            if self.action is DecisionAction.BUY_CE and self.option_type != "CE":
                raise ValueError("BUY_CE requires CE")
            if self.action is DecisionAction.BUY_PE and self.option_type != "PE":
                raise ValueError("BUY_PE requires PE")
        if self.schema_version != SIGNAL_SCHEMA:
            raise ValueError("unsupported signal schema")
        object.__setattr__(self, "supporting_evidence", tuple(self.supporting_evidence))
        object.__setattr__(self, "counter_evidence", tuple(self.counter_evidence))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "action": self.action.value,
            "direction": self.direction,
            "underlying": self.underlying,
            "instrument": self.instrument,
            "option_type": self.option_type,
            "explanation": self.explanation,
            "supporting_evidence": list(self.supporting_evidence),
            "counter_evidence": [row.to_dict() for row in self.counter_evidence],
            "reason_codes": list(self.reason_codes),
            "package_digest": self.package_digest,
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "analysis_cycle_id": self.analysis_cycle_id,
            "engine_version": self.engine_version,
            "schema_version": self.schema_version,
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
        }


def build_signal_id(payload: Mapping[str, Any]) -> str:
    return "sig-" + digest_payload(plain_data(dict(payload)))
