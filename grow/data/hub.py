"""DataHub — fixture snapshots only in Milestone 2A.

Not wired into GrowRuntime. Not a broker. Option chains stay unimplemented.
"""

from __future__ import annotations

from datetime import datetime

from grow.clock import Clock, SystemClock
from grow.config import GrowConfig, load_config
from grow.data.actions import IdentityAdjuster
from grow.data.boundary import research_view
from grow.data.fixture import FixtureSource
from grow.data.schema import BarSeries, MarketSnapshot, ResearchView, Timeframe
from grow.errors import GrowConfigError, GrowInterfaceNotImplemented


class DataHub:
    def __init__(
        self,
        config: GrowConfig | None = None,
        clock: Clock | None = None,
        *,
        source: FixtureSource | None = None,
    ) -> None:
        self.config = config or load_config()
        if self.config.data.provider != "fixture":
            raise GrowConfigError("2A DataHub only constructs the fixture source.")
        if self.config.data.allow_live_feed:
            raise GrowConfigError("Live feeds are not attached.")
        self.clock = clock or SystemClock()
        self.source = source or FixtureSource(self.config, clock=self.clock)
        self.adjuster = IdentityAdjuster()

    def quote(self, ticker: str, as_of: datetime | None = None) -> float:
        return self.snapshot(ticker, as_of=as_of).last_price

    def snapshot(self, ticker: str, as_of: datetime | None = None) -> MarketSnapshot:
        snap = self.source.snapshot(ticker, as_of=as_of)
        for series in snap.series.values():
            self.adjuster.apply(series, ())
        return snap

    def bars(
        self,
        ticker: str,
        timeframe: str | Timeframe,
        as_of: datetime | None = None,
    ) -> BarSeries:
        tf = timeframe if isinstance(timeframe, Timeframe) else Timeframe(str(timeframe).upper())
        snap = self.snapshot(ticker, as_of=as_of)
        return snap.series[tf]

    def research_view(self, ticker: str, as_of: datetime | None = None) -> ResearchView:
        return research_view(self.snapshot(ticker, as_of=as_of))

    def option_chain(self, symbol: str) -> None:
        raise GrowInterfaceNotImplemented("grow.data.option_chain", "after cash data + 2B signals")
