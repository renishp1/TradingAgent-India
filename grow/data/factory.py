"""Construct a DataHub without making DataHub import FixtureSource."""

from __future__ import annotations

from grow.clock import Clock
from grow.config import GrowConfig, load_config
from grow.data.hub import DataHub
from grow.data.source import MarketDataSource
from grow.errors import GrowConfigError


def open_data_hub(
    config: GrowConfig | None = None,
    clock: Clock | None = None,
    *,
    source: MarketDataSource | None = None,
    adjuster=None,
) -> DataHub:
    cfg = config or load_config()
    if source is None:
        if cfg.data.provider != "fixture":
            raise GrowConfigError("2A only constructs the fixture source.")
        from grow.data.fixture import FixtureSource

        source = FixtureSource(cfg, clock=clock)
    return DataHub(cfg, clock, source=source, adjuster=adjuster)
