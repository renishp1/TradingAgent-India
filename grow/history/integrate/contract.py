"""2J provider adapter contract. Historical only. No live execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Protocol

ADAPTER_VERSION = "history.integrate.v1"
RECORDED_PROVIDER_ID = "recorded.nse_fo.v1"
VENDOR_SCHEMA = "nse.fo.recorded.v1"
LIFECYCLE = (
    "RAW_ACQUIRED",
    "NORMALIZED",
    "PIT_VALIDATED",
    "QUALIFICATION_REVIEW",
    "QUALIFIED",
    "REJECTED",
)


@dataclass(frozen=True)
class AcquireScope:
    underlyings: tuple[str, ...]
    start: date
    end: date
    instrument_type: str = "OPTIDX"
    fields: tuple[str, ...] = ("ohlcv", "bid", "ask", "oi", "volume", "lot_size")
    provider_id: str = RECORDED_PROVIDER_ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlyings": list(self.underlyings),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "instrument_type": self.instrument_type,
            "fields": list(self.fields),
            "provider_id": self.provider_id,
        }


@dataclass(frozen=True)
class RawArtifact:
    provider_id: str
    adapter_version: str
    vendor_schema: str
    fingerprint: str
    retrieved_at: str
    scope: AcquireScope
    payload: Mapping[str, Any]
    retry_count: int = 0
    rate_limit_remaining: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "adapter_version": self.adapter_version,
            "vendor_schema": self.vendor_schema,
            "fingerprint": self.fingerprint,
            "retrieved_at": self.retrieved_at,
            "scope": self.scope.to_dict(),
            "retry_count": self.retry_count,
            "rate_limit_remaining": self.rate_limit_remaining,
            "headers": dict(self.headers),
            "live": False,
        }


class HistoricalProvider(Protocol):
    identity: str
    adapter_version: str

    def acquire(self, scope: AcquireScope, **kwargs: Any) -> RawArtifact: ...
