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
from grow.market_data.snapshots.builder import (
    SnapshotBuildError,
    build_agent_snapshot,
    gate_snapshot_quality,
)

__all__ = [
    "AgentMarketSnapshot",
    "DataQualityStatus",
    "OptionQuoteView",
    "SnapshotBuildError",
    "UnderlyingQuoteView",
    "build_agent_snapshot",
    "gate_snapshot_quality",
]
