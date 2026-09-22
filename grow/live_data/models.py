"""Normalized 3A live-stream records. Paper only. Not broker messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping

from grow.data.schema import MarketSnapshot
from grow.options.models import OptionChainSnapshot
from grow.types import Fill, RiskVerdict


SCHEMA = "grow.stream.snapshot.v1"
ADAPTER_VERSION = "live_data.adapter.v1"
MOCK_PROVIDER_ID = "grow.stream.mock.v1"
TRUEDATA_PROVIDER_ID = "grow.stream.truedata.v1"
KITE_MARKET_PROVIDER_ID = "grow.stream.kite.market.v1"
APPROVED_STREAM_IDS = frozenset({MOCK_PROVIDER_ID, TRUEDATA_PROVIDER_ID, KITE_MARKET_PROVIDER_ID})


class SessionHealth(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    READY = "READY"
    RUNNING = "RUNNING"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"


class CycleStatus(str, Enum):
    PAPER_FILL = "PAPER_FILL"
    PAPER_CLOSE = "PAPER_CLOSE"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class LiveHealth:
    state: SessionHealth
    provider_id: str
    adapter_version: str
    last_message_at: datetime | None
    last_sequence: int | None
    error: str | None = None
    reconnect_count: int = 0
    subscribed: tuple[str, ...] = ()
    last_heartbeat_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "provider_id": self.provider_id,
            "adapter_version": self.adapter_version,
            "last_message_at": None if self.last_message_at is None else self.last_message_at.isoformat(),
            "last_sequence": self.last_sequence,
            "error": self.error,
            "reconnect_count": self.reconnect_count,
            "subscribed": list(self.subscribed),
            "last_heartbeat_at": None if self.last_heartbeat_at is None else self.last_heartbeat_at.isoformat(),
            "live_trading": False,
        }


@dataclass(frozen=True)
class LiveContract:
    underlying: str
    expiry: date
    strike: float
    option_type: str
    expiry_class: str
    provider_contract_id: str
    lot_size: int | None
    bid: float | None
    ask: float | None
    last_price: float | None
    volume: int | None
    open_interest: int | None
    timestamp: datetime

    def identity(self) -> tuple[str, str, float, str]:
        return (self.underlying, self.expiry.isoformat(), self.strike, self.option_type)

    def canonical_id(self) -> str:
        return f"{self.underlying}-{self.expiry.isoformat()}-{int(self.strike)}-{self.option_type}"


@dataclass(frozen=True)
class LiveSnapshot:
    snapshot_id: str
    schema: str
    provider_id: str
    adapter_version: str
    sequence: int
    event_time: datetime
    received_time: datetime
    session_date: date
    underlyings: tuple[str, ...]
    market: Mapping[str, MarketSnapshot]
    chains: Mapping[str, OptionChainSnapshot]
    lot_sizes: Mapping[str, int]
    freshness_ok: bool
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "schema": self.schema,
            "provider_id": self.provider_id,
            "adapter_version": self.adapter_version,
            "sequence": self.sequence,
            "event_time": self.event_time.isoformat(),
            "received_time": self.received_time.isoformat(),
            "session_date": self.session_date.isoformat(),
            "underlyings": list(self.underlyings),
            "option_count": sum(len(chain.contracts) for chain in self.chains.values()),
            "freshness_ok": self.freshness_ok,
            "diagnostics": list(self.diagnostics),
            "is_fixture": False,
            "live_trading": False,
        }


@dataclass(frozen=True)
class LiveCycleReport:
    session_id: str
    event_id: str
    sequence: int
    as_of: datetime
    underlying: str
    status: CycleStatus
    reason: str
    provider_id: str
    snapshot_id: str | None
    candidate_id: str | None
    decision_id: str | None
    option_type: str | None
    expiry: str | None
    strike: float | None
    lots: int | None
    lot_size: int | None
    fill: Fill | None
    verdict: RiskVerdict | None
    health: SessionHealth
    extras: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "as_of": self.as_of.isoformat(),
            "underlying": self.underlying,
            "status": self.status.value,
            "reason": self.reason,
            "provider_id": self.provider_id,
            "snapshot_id": self.snapshot_id,
            "candidate_id": self.candidate_id,
            "decision_id": self.decision_id,
            "option_type": self.option_type,
            "expiry": self.expiry,
            "strike": self.strike,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "fill": None if self.fill is None else self.fill.to_dict(),
            "verdict": None if self.verdict is None else self.verdict.to_dict(),
            "health": self.health.value,
            "paper_only": True,
            "live_trading": False,
            "extras": dict(self.extras),
        }


@dataclass
class LiveSessionRecord:
    session_id: str
    provider_id: str
    adapter_version: str
    started_at: datetime
    ended_at: datetime | None = None
    transitions: list[tuple[str, str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "provider_id": self.provider_id,
            "adapter_version": self.adapter_version,
            "started_at": self.started_at.isoformat(),
            "ended_at": None if self.ended_at is None else self.ended_at.isoformat(),
            "transitions": [{"at": at, "from": src, "to": dst} for at, src, dst in self.transitions],
            "live_trading": False,
        }
