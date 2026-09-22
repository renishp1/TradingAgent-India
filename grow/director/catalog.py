"""Approved dataset catalog. CEO cannot invent providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

from grow.backtest.calendar import FIXTURE_CALENDAR
from grow.errors import GrowConfigError

APPROVED = "APPROVED"
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
        }


def default_catalog() -> dict[str, ApprovedDataSource]:
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
    )
    return {fixture.dataset_id: fixture, stub.dataset_id: stub}


def require_approved(catalog: Mapping[str, ApprovedDataSource], dataset_id: str) -> ApprovedDataSource:
    source = catalog.get(dataset_id)
    if source is None:
        raise GrowConfigError(f"UNKNOWN_DATASET:{dataset_id}")
    if source.licensing_status != APPROVED:
        raise GrowConfigError(f"DATASET_NOT_APPROVED:{dataset_id}")
    return source
