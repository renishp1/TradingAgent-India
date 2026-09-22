"""Milestone 2J — recorded historical provider integration. No live feed. No broker."""

from grow.history.integrate.contract import (
    ADAPTER_VERSION,
    RECORDED_PROVIDER_ID,
    VENDOR_SCHEMA,
    AcquireScope,
    RawArtifact,
)
from grow.history.integrate.pipeline import IntegrationResult, ingest
from grow.history.integrate.recorded import RecordedHistoricalProvider, open_provider
from grow.history.integrate.replay import (
    bind_director_catalog,
    catalog_row,
    historical_config,
    open_qualified_runner,
    require_qualified_real,
)
from grow.history.integrate.sample import recorded_payload, recorded_scope

__all__ = [
    "ADAPTER_VERSION",
    "RECORDED_PROVIDER_ID",
    "VENDOR_SCHEMA",
    "AcquireScope",
    "RawArtifact",
    "IntegrationResult",
    "ingest",
    "RecordedHistoricalProvider",
    "open_provider",
    "bind_director_catalog",
    "catalog_row",
    "historical_config",
    "open_qualified_runner",
    "require_qualified_real",
    "recorded_payload",
    "recorded_scope",
]
