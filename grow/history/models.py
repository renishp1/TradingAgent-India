"""2G canonical historical records. Not live quotes. Not trades."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

ALLOWED_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY"})
ALLOWED_OPTION_TYPES = frozenset({"CE", "PE"})
SCHEMA = "grow.history.canonical.v1"
NORM = "history.normalize.v1"

DRAFT = "DRAFT"
VALIDATING = "VALIDATING"
APPROVED = "APPROVED"
APPROVED_WITH_WARNINGS = "APPROVED_WITH_WARNINGS"
REJECTED = "REJECTED"
RETIRED = "RETIRED"
SYNTHETIC = "SYNTHETIC"
FRAMEWORK_TEST_ONLY = "FRAMEWORK_TEST_ONLY"
HISTORICAL_RESEARCH = "HISTORICAL_RESEARCH"

MISSING_IV = "MISSING_IV"
BID_ASK_GAPS = "BID_ASK_GAPS"
OPTION_SNAPSHOT_GAPS = "OPTION_SNAPSHOT_GAPS"
MISSING_OI = "MISSING_OI"
MISSING_VOLUME = "MISSING_VOLUME"
MISSING_SESSIONS = "MISSING_SESSIONS"
KNOWN_QUALITY_WARNINGS = frozenset(
    {MISSING_IV, BID_ASK_GAPS, OPTION_SNAPSHOT_GAPS, MISSING_OI, MISSING_VOLUME, MISSING_SESSIONS}
)

MAPPING_EXACT = "EXACT"
MAPPING_NEAREST = "NEAREST_WITHIN_TOLERANCE"
KNOWN_MAPPING_POLICIES = frozenset({MAPPING_EXACT, MAPPING_NEAREST})


@dataclass(frozen=True)
class HistoricalSession:
    session_date: date
    open_at: datetime
    close_at: datetime
    source: str
    calendar_version: str
    status: str
    special_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date.isoformat(),
            "open_at": self.open_at.isoformat(),
            "close_at": self.close_at.isoformat(),
            "source": self.source,
            "calendar_version": self.calendar_version,
            "status": self.status,
            "special_reason": self.special_reason,
        }


@dataclass(frozen=True)
class HistoricalBar:
    symbol: str
    timeframe: str
    timestamp: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    source_id: str
    dataset_version: str
    as_of_available_at: datetime
    corporate_action_adjustment_version: str
    quality_flags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp.isoformat(),
            "end": self.end.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "source_id": self.source_id,
            "dataset_version": self.dataset_version,
            "as_of_available_at": self.as_of_available_at.isoformat(),
            "corporate_action_adjustment_version": self.corporate_action_adjustment_version,
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True)
class HistoricalOptionContract:
    underlying: str
    expiry: date
    strike: float
    option_type: str
    contract_id: str
    provider_contract_id: str
    lot_size: int | None
    expiry_class: str
    first_seen_at: datetime
    last_seen_at: datetime
    listing_status: str
    source_id: str
    dataset_version: str

    def identity(self) -> tuple[str, str, float, str]:
        return (self.underlying, self.expiry.isoformat(), self.strike, self.option_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type,
            "contract_id": self.contract_id,
            "provider_contract_id": self.provider_contract_id,
            "lot_size": self.lot_size,
            "expiry_class": self.expiry_class,
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "listing_status": self.listing_status,
            "source_id": self.source_id,
            "dataset_version": self.dataset_version,
        }


@dataclass(frozen=True)
class HistoricalOptionQuote:
    contract_id: str
    timestamp: datetime
    bid: float | None
    ask: float | None
    ltp: float | None
    volume: int | None
    open_interest: int | None
    previous_open_interest: int | None
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    greek_source: str | None
    iv_source: str | None
    source_id: str
    dataset_version: str
    as_of_available_at: datetime
    quality_flags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "timestamp": self.timestamp.isoformat(),
            "bid": self.bid,
            "ask": self.ask,
            "ltp": self.ltp,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "previous_open_interest": self.previous_open_interest,
            "implied_volatility": self.implied_volatility,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
            "greek_source": self.greek_source,
            "iv_source": self.iv_source,
            "source_id": self.source_id,
            "dataset_version": self.dataset_version,
            "as_of_available_at": self.as_of_available_at.isoformat(),
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True)
class DatasetVersion:
    dataset_id: str
    version: str
    source_version: str
    normalization_version: str
    schema_version: str
    calendar_version: str
    coverage_start: date
    coverage_end: date
    fingerprint: str
    published_at: str
    quality_status: str
    license_status: str
    source_id: str
    provider_name: str
    granularity: tuple[str, ...]
    instrument_scope: tuple[str, ...]
    bid_ask_available: bool
    oi_available: bool
    volume_available: bool
    iv_available: bool
    greeks_available: bool
    contract_metadata_available: bool
    option_depth: str
    provenance: str
    timezone: str
    usage_scope: str
    is_fixture: bool
    snapshot_cadence: tuple[str, ...]
    quality_warnings: tuple[str, ...]
    mapping_policy: str
    slot_tolerance_seconds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "version": self.version,
            "source_version": self.source_version,
            "normalization_version": self.normalization_version,
            "schema_version": self.schema_version,
            "calendar_version": self.calendar_version,
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
            "fingerprint": self.fingerprint,
            "published_at": self.published_at,
            "quality_status": self.quality_status,
            "license_status": self.license_status,
            "source_id": self.source_id,
            "provider_name": self.provider_name,
            "granularity": list(self.granularity),
            "instrument_scope": list(self.instrument_scope),
            "bid_ask_available": self.bid_ask_available,
            "oi_available": self.oi_available,
            "volume_available": self.volume_available,
            "iv_available": self.iv_available,
            "greeks_available": self.greeks_available,
            "contract_metadata_available": self.contract_metadata_available,
            "option_depth": self.option_depth,
            "provenance": self.provenance,
            "timezone": self.timezone,
            "usage_scope": self.usage_scope,
            "is_fixture": self.is_fixture,
            "snapshot_cadence": list(self.snapshot_cadence),
            "quality_warnings": list(self.quality_warnings),
            "mapping_policy": self.mapping_policy,
            "slot_tolerance_seconds": self.slot_tolerance_seconds,
            "label": "HISTORICAL DATA / RESEARCH ONLY",
        }


@dataclass(frozen=True)
class CoverageReport:
    dataset_version: str
    expected_sessions: int
    actual_sessions: int
    missing_sessions: tuple[str, ...]
    missing_bar_intervals: tuple[str, ...]
    missing_option_snapshots: tuple[str, ...]
    expected_quotes: int
    observed_quotes: int
    quote_completeness: float
    bid_ask_completeness: float
    oi_completeness: float
    volume_completeness: float
    iv_completeness: float
    greek_completeness: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_version": self.dataset_version,
            "expected_sessions": self.expected_sessions,
            "actual_sessions": self.actual_sessions,
            "missing_sessions": list(self.missing_sessions),
            "missing_bar_intervals": list(self.missing_bar_intervals),
            "missing_option_snapshots": list(self.missing_option_snapshots),
            "expected_quotes": self.expected_quotes,
            "observed_quotes": self.observed_quotes,
            "quote_completeness": self.quote_completeness,
            "bid_ask_completeness": self.bid_ask_completeness,
            "oi_completeness": self.oi_completeness,
            "volume_completeness": self.volume_completeness,
            "iv_completeness": self.iv_completeness,
            "greek_completeness": self.greek_completeness,
        }
