"""Explicit LIVE / FIXTURE / MIXED market-data provenance.

Paper execution may use LIVE or FIXTURE snapshots when the campaign allows
them. MIXED (for example fixture CE + live PE) is always rejected with
``MIXED_MARKET_DATA_SOURCE``. Never classify a mixed book as fully LIVE.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable, Sequence

MIXED_MARKET_DATA_SOURCE = "MIXED_MARKET_DATA_SOURCE"


class MarketDataSource(str, Enum):
    LIVE = "LIVE"
    FIXTURE = "FIXTURE"
    MIXED = "MIXED"


def classify_fixture_flags(flags: Sequence[bool] | Iterable[bool]) -> MarketDataSource:
    """Classify a set of per-quote/per-chain fixture flags.

    Empty input is LIVE (no option quotes to mix). Any True+False pair is MIXED.
    """
    rows = tuple(bool(flag) for flag in flags)
    if not rows:
        return MarketDataSource.LIVE
    if all(rows):
        return MarketDataSource.FIXTURE
    if any(rows):
        return MarketDataSource.MIXED
    return MarketDataSource.LIVE


def classify_agent_snapshot(snapshot) -> MarketDataSource:
    """Derive provenance from option quotes, falling back to snapshot.is_fixture."""
    contracts = tuple(getattr(snapshot, "option_contracts", ()) or ())
    if contracts:
        return classify_fixture_flags(bool(getattr(row, "is_fixture", False)) for row in contracts)
    if bool(getattr(snapshot, "is_fixture", False)):
        return MarketDataSource.FIXTURE
    diagnostics = dict(getattr(snapshot, "diagnostics", {}) or {})
    raw = diagnostics.get("market_data_source")
    if raw in {item.value for item in MarketDataSource}:
        return MarketDataSource(str(raw))
    return MarketDataSource.LIVE


def reject_mixed_market_data(snapshot) -> str | None:
    """Return ``MIXED_MARKET_DATA_SOURCE`` when paper must refuse this snapshot."""
    source = getattr(snapshot, "market_data_source", None)
    if source is None:
        source = classify_agent_snapshot(snapshot)
    elif not isinstance(source, MarketDataSource):
        try:
            source = MarketDataSource(str(source))
        except ValueError:
            source = classify_agent_snapshot(snapshot)
    if source is MarketDataSource.MIXED:
        return MIXED_MARKET_DATA_SOURCE
    return None
