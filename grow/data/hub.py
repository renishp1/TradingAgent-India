"""DataHub — depends on MarketDataSource, not a concrete feed.

Default fixture wiring lives in grow.data.factory.open_data_hub.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from grow.clock import Clock, SystemClock
from grow.config import GrowConfig
from grow.data.actions import IdentityAdjuster
from grow.data.boundary import research_view
from grow.data.schema import BarSeries, MarketSnapshot, ResearchView, Timeframe
from grow.data.source import MarketDataSource
from grow.errors import GrowConfigError, GrowInterfaceNotImplemented


class DataHub:
    def __init__(
        self,
        config: GrowConfig,
        clock: Clock | None = None,
        *,
        source: MarketDataSource,
        adjuster: IdentityAdjuster | None = None,
    ) -> None:
        self.config = config
        if self.config.data.allow_live_feed:
            raise GrowConfigError("Live feeds are not attached.")
        if source.meta().is_live:
            raise GrowConfigError("Live feeds are not attached.")
        self.clock = clock or SystemClock()
        self.source = source
        self.adjuster = adjuster or IdentityAdjuster()

    def quote(self, ticker: str, as_of: datetime | None = None) -> float:
        return self.snapshot(ticker, as_of=as_of).last_price

    def snapshot(self, ticker: str, as_of: datetime | None = None) -> MarketSnapshot:
        snap = self.source.snapshot(ticker, as_of=as_of)
        series = {tf: self.adjuster.apply(bars, ()) for tf, bars in snap.series.items()}
        return replace(snap, series=series)

    def bars(
        self,
        ticker: str,
        timeframe: str | Timeframe,
        as_of: datetime | None = None,
    ) -> BarSeries:
        tf = timeframe if isinstance(timeframe, Timeframe) else Timeframe(str(timeframe).upper())
        return self.snapshot(ticker, as_of=as_of).series[tf]

    def research_view(self, ticker: str, as_of: datetime | None = None) -> ResearchView:
        return research_view(self.snapshot(ticker, as_of=as_of))

    def option_chain(self, symbol: str) -> None:
        raise GrowInterfaceNotImplemented("grow.data.option_chain", "after cash data + 2B signals")
