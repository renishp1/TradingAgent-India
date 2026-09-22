"""2E/2F consumption of 2J QUALIFIED datasets. No fixture fallback."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from grow.backtest.runner import BacktestRunner
from grow.config import GrowConfig, load_config
from grow.director.catalog import APPROVED, ApprovedDataSource
from grow.errors import GrowConfigError
from grow.history.bridge import HistoricalMarketSource, HistoricalOptionSource
from grow.history.calendar import calendar_for
from grow.history.eval import DatasetQualificationRecord
from grow.history.models import APPROVED_FOR_2E, FRAMEWORK_TEST_ONLY, QUALIFIED
from grow.history.store import CanonicalStore

READY = frozenset({QUALIFIED, APPROVED_FOR_2E})


def require_qualified_real(store: CanonicalStore, record: DatasetQualificationRecord) -> None:
    if store.meta.is_fixture or store.meta.usage_scope == FRAMEWORK_TEST_ONLY:
        raise GrowConfigError("FIXTURE_FALLBACK_FORBIDDEN")
    if record.dataset_id != store.meta.dataset_id:
        raise GrowConfigError("QUALIFICATION_DATASET_MISMATCH")
    if record.dataset_version != store.meta.version:
        raise GrowConfigError("QUALIFICATION_VERSION_MISMATCH")
    if record.fingerprint != store.meta.fingerprint:
        raise GrowConfigError("QUALIFICATION_FINGERPRINT_MISMATCH")
    if record.qualification_status not in READY:
        raise GrowConfigError(f"DATASET_NOT_QUALIFIED:{record.qualification_status}")


def historical_config(base: GrowConfig | None = None) -> GrowConfig:
    cfg = base or load_config()
    if cfg.backtest.provider != "fixture":
        raise GrowConfigError("2E backtest.provider must be fixture.")
    return replace(
        cfg,
        options=replace(cfg.options, provider="historical", allow_live_chain=False),
    )


def catalog_row(store: CanonicalStore, record: DatasetQualificationRecord) -> ApprovedDataSource:
    require_qualified_real(store, record)
    meta = store.meta
    return ApprovedDataSource(
        dataset_id=meta.dataset_id,
        provider=meta.provider_name,
        instrument_scope=meta.instrument_scope,
        date_coverage=(meta.coverage_start, meta.coverage_end),
        timestamp_granularity=meta.granularity,
        timezone=meta.timezone,
        option_chain_depth=meta.option_depth,
        bid_ask_available=meta.bid_ask_available,
        oi_available=meta.oi_available,
        volume_available=meta.volume_available,
        iv_available=meta.iv_available,
        greeks_available=meta.greeks_available,
        historical_contract_metadata=meta.contract_metadata_available,
        session_calendar_version=meta.calendar_version,
        quality_status=meta.quality_status,
        licensing_status=APPROVED if meta.license_status == "APPROVED" else meta.license_status,
        dataset_version=meta.version,
        provenance=meta.provenance,
        usage_scope=meta.usage_scope,
        is_fixture=False,
        quality_warnings=record.quality_warnings,
        qualification_status=record.qualification_status,
        fingerprint=meta.fingerprint,
    )


def bind_director_catalog(director, store: CanonicalStore, record: DatasetQualificationRecord) -> ApprovedDataSource:
    row = catalog_row(store, record)
    director.catalog[row.dataset_id] = row
    return row


def open_qualified_runner(
    store: CanonicalStore,
    record: DatasetQualificationRecord,
    *,
    start: date,
    end: date,
    config: GrowConfig | None = None,
) -> BacktestRunner:
    require_qualified_real(store, record)
    cfg = historical_config(config)
    if cfg.options.provider != "historical":
        raise GrowConfigError("HISTORICAL_PROVIDER_REQUIRED")
    if cfg.options.allow_live_chain:
        raise GrowConfigError("LIVE_CHAIN_FORBIDDEN")
    if cfg.backtest.provider != "fixture":
        raise GrowConfigError("2E backtest.provider must be fixture.")
    return BacktestRunner(
        cfg,
        calendar=calendar_for(store, start, end),
        market_source=HistoricalMarketSource(store, cfg),
        option_source=HistoricalOptionSource(store),
    )
