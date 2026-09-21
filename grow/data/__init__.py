"""Market data layer — Milestone 2A.

Default source is an in-process fixture. Licensed feeds, HTML scrapes, and
broker quotes are refuse-closed. This package does not emit trades.
"""

from grow.data.boundary import assert_research_payload, research_view
from grow.data.hub import DataHub
from grow.data.licensed import LicensedFeed
from grow.data.source import MarketDataSource
from grow.data.schema import (
    FIXTURE_SOURCE,
    Bar,
    BarSeries,
    MarketSnapshot,
    ResearchView,
    SourceMeta,
    Timeframe,
)
from grow.data.universe import NIFTY50_EQUITIES, NIFTY_INDICES, data_universe

__all__ = [
    "FIXTURE_SOURCE",
    "Bar",
    "BarSeries",
    "DataHub",
    "LicensedFeed",
    "MarketDataSource",
    "MarketSnapshot",
    "NIFTY50_EQUITIES",
    "NIFTY_INDICES",
    "ResearchView",
    "SourceMeta",
    "Timeframe",
    "assert_research_payload",
    "data_universe",
    "research_view",
]
