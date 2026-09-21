"""Market-data source contract.

2A constructs only FixtureSource. A licensed adapter must satisfy this
protocol and still emit Bar / MarketSnapshot — never vendor JSON.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from grow.data.schema import BarSeries, MarketSnapshot, SourceMeta, Timeframe


@runtime_checkable
class MarketDataSource(Protocol):
    def meta(self) -> SourceMeta: ...

    def snapshot(self, ticker: str, as_of: datetime | None = None) -> MarketSnapshot: ...

    def bars(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        session_day: date,
        as_of: datetime | None = None,
        history_sessions: int | None = None,
    ) -> BarSeries: ...
