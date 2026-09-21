"""Quant vs research boundary.

Raw OHLCV stays in quantitative code. LLM research receives ResearchView.
"""

from __future__ import annotations

from typing import Any, Mapping

from grow.data.schema import MarketSnapshot, ResearchView
from grow.errors import GrowSafetyError

_FORBIDDEN_RESEARCH_KEYS = frozenset(
    {
        "bars",
        "ohlcv",
        "candles",
        "series",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }
)


def research_view(snapshot: MarketSnapshot) -> ResearchView:
    counts = {tf.value: len(series.bars) for tf, series in snapshot.series.items()}
    notes = (
        "ResearchView contains no OHLCV arrays.",
        f"source={snapshot.source.name}",
        f"fixture={snapshot.source.is_fixture}",
        *snapshot.quality.notes,
    )
    return ResearchView(
        snapshot_id=snapshot.snapshot_id,
        symbol=snapshot.symbol,
        as_of=snapshot.as_of,
        session=snapshot.session,
        last_price=snapshot.last_price,
        currency=snapshot.currency,
        source_name=snapshot.source.name,
        is_fixture=snapshot.source.is_fixture,
        quality_complete=snapshot.quality.complete,
        quality_stale=snapshot.quality.stale,
        missing_count=snapshot.quality.missing_count,
        bar_counts=counts,
        notes=notes,
    )


def assert_research_payload(payload: Any) -> None:
    """Fail closed if raw candles leak into an LLM payload at any depth."""
    if isinstance(payload, Mapping):
        leaked = {str(key).lower() for key in payload} & _FORBIDDEN_RESEARCH_KEYS
        if leaked:
            raise GrowSafetyError(
                f"Research payload contains raw market-data keys: {sorted(leaked)}"
            )
        for value in payload.values():
            assert_research_payload(value)
        return
    if isinstance(payload, (list, tuple, set, frozenset)):
        for item in payload:
            assert_research_payload(item)
        return
