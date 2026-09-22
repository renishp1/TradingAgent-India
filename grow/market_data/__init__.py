"""Provider-neutral market-data surface for multi-agent analysis.

Agents consume versioned snapshots from this package. Provider adapters stay in
``grow.live_data`` (and later adapters). Broker SDKs are never imported here.
"""

from grow.market_data.normalized.models import (
    AgentMarketSnapshot,
    DataQualityStatus,
    OptionQuoteView,
    UnderlyingQuoteView,
)
from grow.market_data.provenance import (
    MIXED_MARKET_DATA_SOURCE,
    MarketDataSource,
    classify_agent_snapshot,
    classify_fixture_flags,
    reject_mixed_market_data,
)
from grow.market_data.snapshots.builder import (
    SnapshotBuildError,
    build_agent_snapshot,
    gate_snapshot_quality,
)

__all__ = [
    "AgentMarketSnapshot",
    "DataQualityStatus",
    "MIXED_MARKET_DATA_SOURCE",
    "MarketDataSource",
    "OptionQuoteView",
    "SnapshotBuildError",
    "UnderlyingQuoteView",
    "build_agent_snapshot",
    "classify_agent_snapshot",
    "classify_fixture_flags",
    "gate_snapshot_quality",
    "reject_mixed_market_data",
]
