"""Approved dataset catalog. CEO cannot invent providers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Mapping

from grow.backtest.calendar import FIXTURE_CALENDAR
from grow.errors import GrowConfigError

APPROVED = "APPROVED"
APPROVED_WITH_WARNINGS = "APPROVED_WITH_WARNINGS"
NOT_APPROVED = "NOT_APPROVED"
FIXTURE_DATASET = "grow.data.fixture.v1"
UNAPPROVED_STUB = "grow.data.unapproved.stub.v1"


@dataclass(frozen=True)
class ApprovedDataSource:
    dataset_id: str
    provider: str
    instrument_scope: tuple[str, ...]
    date_coverage: tuple[date, date]
    timestamp_granularity: tuple[str, ...]
    timezone: str
    option_chain_depth: str
    bid_ask_available: bool
    oi_available: bool
    volume_available: bool
    iv_available: bool
    greeks_available: bool
    historical_contract_metadata: bool
    session_calendar_version: str
    quality_status: str
    licensing_status: str
    dataset_version: str
    provenance: str
    usage_scope: str
    is_fixture: bool
    quality_warnings: tuple[str, ...] = ()
    qualification_status: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "provider": self.provider,
            "instrument_scope": list(self.instrument_scope),
            "date_coverage": [self.date_coverage[0].isoformat(), self.date_coverage[1].isoformat()],
            "timestamp_granularity": list(self.timestamp_granularity),
            "timezone": self.timezone,
            "option_chain_depth": self.option_chain_depth,
            "bid_ask_available": self.bid_ask_available,
            "oi_available": self.oi_available,
            "volume_available": self.volume_available,
            "iv_available": self.iv_available,
            "greeks_available": self.greeks_available,
            "historical_contract_metadata": self.historical_contract_metadata,
            "session_calendar_version": self.session_calendar_version,
            "quality_status": self.quality_status,
            "licensing_status": self.licensing_status,
            "dataset_version": self.dataset_version,
            "provenance": self.provenance,
            "usage_scope": self.usage_scope,
            "is_fixture": self.is_fixture,
            "quality_warnings": list(self.quality_warnings),
            "qualification_status": self.qualification_status,
        }


def _base_catalog() -> dict[str, ApprovedDataSource]:
    fixture = ApprovedDataSource(
        dataset_id=FIXTURE_DATASET,
        provider="fixture",
        instrument_scope=("NIFTY", "BANKNIFTY"),
        date_coverage=(date(2026, 1, 1), date(2026, 12, 31)),
        timestamp_granularity=("M5", "M15", "D1"),
        timezone="Asia/Kolkata",
        option_chain_depth="atm_pm2",
        bid_ask_available=True,
        oi_available=True,
        volume_available=True,
        iv_available=True,
        greeks_available=True,
        historical_contract_metadata=True,
        session_calendar_version=FIXTURE_CALENDAR,
        quality_status="synthetic",
        licensing_status=APPROVED,
        dataset_version=FIXTURE_DATASET,
        provenance="grow.data.fixture + grow.options.fixture",
        usage_scope="FRAMEWORK_TEST_ONLY",
        is_fixture=True,
    )
    stub = ApprovedDataSource(
        dataset_id=UNAPPROVED_STUB,
        provider="unverified",
        instrument_scope=("NIFTY",),
        date_coverage=(date(2020, 1, 1), date(2020, 12, 31)),
        timestamp_granularity=("D1",),
        timezone="Asia/Kolkata",
        option_chain_depth="none",
        bid_ask_available=False,
        oi_available=False,
        volume_available=False,
        iv_available=False,
        greeks_available=False,
        historical_contract_metadata=False,
        session_calendar_version="unknown",
        quality_status="unreviewed",
        licensing_status=NOT_APPROVED,
        dataset_version=UNAPPROVED_STUB,
        provenance="rejection-stub",
        usage_scope="FRAMEWORK_TEST_ONLY",
        is_fixture=True,
    )
    return {fixture.dataset_id: fixture, stub.dataset_id: stub}


def default_catalog() -> dict[str, ApprovedDataSource]:
    cat = _base_catalog()
    from grow.history.registry import default_registry

    for row in default_registry().catalog_rows():
        cat[row["dataset_id"]] = ApprovedDataSource(
            dataset_id=row["dataset_id"],
            provider=row["provider"],
            instrument_scope=tuple(row["instrument_scope"]),
            date_coverage=row["date_coverage"],
            timestamp_granularity=tuple(row["timestamp_granularity"]),
            timezone=row["timezone"],
            option_chain_depth=row["option_chain_depth"],
            bid_ask_available=row["bid_ask_available"],
            oi_available=row["oi_available"],
            volume_available=row["volume_available"],
            iv_available=row["iv_available"],
            greeks_available=row["greeks_available"],
            historical_contract_metadata=row["historical_contract_metadata"],
            session_calendar_version=row["session_calendar_version"],
            quality_status=row["quality_status"],
            licensing_status=row["licensing_status"],
            dataset_version=row["dataset_version"],
            provenance=row["provenance"],
            usage_scope=row.get("usage_scope", "FRAMEWORK_TEST_ONLY"),
            is_fixture=bool(row.get("is_fixture", True)),
            quality_warnings=tuple(row.get("quality_warnings") or ()),
            qualification_status=str(row.get("qualification_status") or ""),
        )
    return cat


def require_approved(catalog: Mapping[str, ApprovedDataSource], dataset_id: str) -> ApprovedDataSource:
    source = catalog.get(dataset_id)
    if source is None:
        raise GrowConfigError(f"UNKNOWN_DATASET:{dataset_id}")
    if source.licensing_status != APPROVED:
        raise GrowConfigError(f"DATASET_NOT_APPROVED:{dataset_id}")
    return source


def acknowledge_dataset_warnings(
    dataset_warnings: tuple[str, ...] | list[str],
    accepted: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    from grow.history.models import KNOWN_QUALITY_WARNINGS

    issues: list[str] = []
    for wid in list(dataset_warnings) + list(accepted):
        if wid not in KNOWN_QUALITY_WARNINGS:
            issues.append(f"UNKNOWN_WARNING_ID:{wid}")
    material = frozenset(dataset_warnings)
    chosen = frozenset(accepted)
    if not material:
        return tuple(dict.fromkeys(issues))
    unknown = chosen - material
    missing = material - chosen
    if unknown:
        issues.extend(f"UNKNOWN_WARNING_ID:{wid}" for wid in sorted(unknown) if wid in KNOWN_QUALITY_WARNINGS)
    if missing:
        issues.append("WARNINGS_NOT_ACKNOWLEDGED")
    return tuple(dict.fromkeys(issues))


def require_historical_research(
    catalog: Mapping[str, ApprovedDataSource],
    dataset_id: str,
    *,
    accepted_warnings: tuple[str, ...] = (),
) -> ApprovedDataSource:
    source = require_approved(catalog, dataset_id)
    if source.usage_scope != "HISTORICAL_RESEARCH" or source.is_fixture:
        raise GrowConfigError(f"DATASET_FRAMEWORK_ONLY:{dataset_id}")
    if source.quality_status == "APPROVED":
        extra = acknowledge_dataset_warnings(source.quality_warnings, accepted_warnings)
        if extra:
            raise GrowConfigError(";".join(extra))
        return source
    if source.quality_status == APPROVED_WITH_WARNINGS:
        issues = acknowledge_dataset_warnings(source.quality_warnings, accepted_warnings)
        if issues:
            raise GrowConfigError(";".join(issues))
        return source
    raise GrowConfigError(f"DATASET_FRAMEWORK_ONLY:{dataset_id}")


def require_approved_for_2e(
    catalog: Mapping[str, ApprovedDataSource],
    dataset_id: str,
    *,
    accepted_warnings: tuple[str, ...] = (),
) -> ApprovedDataSource:
    source = require_historical_research(catalog, dataset_id, accepted_warnings=accepted_warnings)
    if source.qualification_status != "APPROVED_FOR_2E":
        raise GrowConfigError(f"NOT_APPROVED_FOR_2E:{dataset_id}")
    return source


def consume_qualification(source: ApprovedDataSource, record) -> ApprovedDataSource:
    if source.dataset_id != record.dataset_id:
        raise GrowConfigError("QUALIFICATION_DATASET_MISMATCH")
    if source.dataset_version != record.dataset_version:
        raise GrowConfigError("QUALIFICATION_VERSION_MISMATCH")
    if source.is_fixture or source.usage_scope == "FRAMEWORK_TEST_ONLY":
        if record.approved_for_2e or record.qualification_status == "APPROVED_FOR_2E":
            raise GrowConfigError("FRAMEWORK_TEST_ONLY cannot become APPROVED_FOR_2E")
    return replace(
        source,
        qualification_status=record.qualification_status,
        quality_warnings=record.quality_warnings,
    )
