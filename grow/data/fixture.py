"""Deterministic synthetic OHLCV. This is not a market feed.

Prices are a hash of ticker+session date, same philosophy as the milestone 1
stub quote. Volume is synthetic. `SourceMeta.is_live` is always false.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta

from grow.clock import IST, Clock, SystemClock
from grow.config import GrowConfig
from grow.data.normalizer import normalize_bar
from grow.data.quality import assess_series, combine_quality
from grow.data.schema import (
    FIXTURE_SOURCE,
    Bar,
    BarSeries,
    MarketSnapshot,
    Timeframe,
)
from grow.data.schedule import bar_duration, complete_starts, expected_starts, session_day_for
from grow.data.universe import is_in_data_universe
from grow.errors import GrowConfigError
from grow.market.session import SessionCalendar
from grow.types import Symbol


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _daily_close(ticker: str, session_day: date) -> float:
    digest = _digest(f"{ticker}:{session_day.isoformat()}")
    basis = 400 + (int(digest[:6], 16) % 3600)
    noise = (int(digest[6:10], 16) % 100) / 100.0
    return round(basis + noise, 2)


def _volume(ticker: str, start: datetime, timeframe: Timeframe) -> int:
    digest = _digest(f"vol:{ticker}:{timeframe.value}:{start.isoformat()}")
    base = 50_000 if timeframe is Timeframe.D1 else 4_000 if timeframe is Timeframe.M15 else 1_200
    return base + (int(digest[:5], 16) % base)


def _ohlc_from_close(ticker: str, start: datetime, close: float) -> dict[str, float]:
    digest = _digest(f"ohlc:{ticker}:{start.isoformat()}")
    width = 0.002 + (int(digest[:3], 16) % 40) / 10_000
    drift = ((int(digest[3:6], 16) % 21) - 10) / 10_000
    open_px = round(close * (1 - drift), 2)
    high = round(max(open_px, close) * (1 + width), 2)
    low = round(min(open_px, close) * (1 - width), 2)
    return {"open": open_px, "high": high, "low": low, "close": round(close, 2)}


class FixtureSource:
    """In-process synthetic source. No sockets, no vendors."""

    name = FIXTURE_SOURCE.name

    def __init__(self, config: GrowConfig, clock: Clock | None = None) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self.calendar = SessionCalendar(config.market, clock=self.clock)

    def meta(self):
        return FIXTURE_SOURCE

    def bars(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        session_day: date,
        as_of: datetime | None = None,
        history_sessions: int | None = None,
    ) -> BarSeries:
        symbol = Symbol(ticker=ticker, exchange=self.config.market.exchange)
        if not is_in_data_universe(symbol.ticker):
            raise GrowConfigError(f"{symbol.ticker} is outside the 2A data universe")
        moment = (as_of or self.clock.now()).astimezone(IST)
        open_t = self.calendar.open_time
        close_t = self.calendar.close_time
        if timeframe is Timeframe.D1:
            count = history_sessions if history_sessions is not None else self.config.data.history_sessions
            days = self._completed_session_days(session_day, count, moment)
            built = [self._daily_bar(symbol, day, moment) for day in days]
            return BarSeries(symbol=symbol, timeframe=timeframe, bars=tuple(built))
        starts = complete_starts(
            session_day,
            timeframe,
            moment,
            session_open=open_t,
            session_close=close_t,
        )
        built = [self._intraday_bar(symbol, timeframe, start, session_day) for start in starts]
        return BarSeries(symbol=symbol, timeframe=timeframe, bars=tuple(built))

    def snapshot(self, ticker: str, as_of: datetime | None = None) -> MarketSnapshot:
        moment = (as_of or self.clock.now()).astimezone(IST)
        session_day = session_day_for(self.calendar, moment)
        if session_day is None:
            raise GrowConfigError("no cash session in lookback")
        symbol = Symbol(ticker=ticker, exchange=self.config.market.exchange)
        series: dict[Timeframe, BarSeries] = {}
        qualities = []
        for name in self.config.data.timeframes:
            tf = Timeframe(name)
            series[tf] = self.bars(ticker, tf, session_day=session_day, as_of=moment)
            qualities.append(
                assess_series(
                    series[tf],
                    session_day=session_day,
                    as_of=moment,
                    session_open=self.calendar.open_time,
                    session_close=self.calendar.close_time,
                    stale_after_seconds=self.config.data.stale_after_seconds,
                )
            )
        last = series[Timeframe.D1].bars[-1].close if series[Timeframe.D1].bars else _daily_close(
            symbol.ticker, session_day
        )
        # Prefer last complete intraday close when it exists.
        if Timeframe.M5 in series and series[Timeframe.M5].bars:
            last = series[Timeframe.M5].bars[-1].close
        elif Timeframe.M15 in series and series[Timeframe.M15].bars:
            last = series[Timeframe.M15].bars[-1].close
        quality = combine_quality(tuple(qualities))
        snapshot_id = _digest(f"{symbol.ticker}:{moment.isoformat()}:{FIXTURE_SOURCE.name}")[:16]
        return MarketSnapshot(
            snapshot_id=snapshot_id,
            symbol=symbol,
            as_of=moment,
            session=self.calendar.state(moment),
            last_price=last,
            currency=self.config.market.currency,
            series=series,
            quality=quality,
            source=FIXTURE_SOURCE,
        )

    def _completed_session_days(self, last_day: date, count: int, as_of: datetime) -> tuple[date, ...]:
        days: list[date] = []
        probe = last_day
        while len(days) < count:
            noon = datetime.combine(probe, time(12, 0), tzinfo=IST)
            state = self.calendar.state(noon)
            if state.value in {"OPEN", "SQUARE_OFF_WINDOW", "CLOSED"}:
                if probe < last_day or as_of >= datetime.combine(probe, self.calendar.open_time, tzinfo=IST):
                    days.append(probe)
            probe = probe - timedelta(days=1)
            if (last_day - probe).days > 40:
                break
        days.reverse()
        return tuple(days[-count:])

    def _daily_bar(self, symbol: Symbol, session_day: date, as_of: datetime) -> Bar:
        start = datetime.combine(session_day, self.calendar.open_time, tzinfo=IST)
        session_end = datetime.combine(session_day, self.calendar.close_time, tzinfo=IST)
        as_of = as_of.astimezone(IST)
        forming = session_day == as_of.date() and as_of < session_end
        if forming:
            end = as_of
            intraday = self.bars(
                symbol.ticker,
                Timeframe.M5,
                session_day=session_day,
                as_of=as_of,
            )
            if intraday.bars:
                high = max(bar.high for bar in intraday.bars)
                low = min(bar.low for bar in intraday.bars)
                return Bar(
                    symbol=symbol,
                    timeframe=Timeframe.D1,
                    start=start,
                    end=end,
                    open=intraday.bars[0].open,
                    high=high,
                    low=low,
                    close=intraday.bars[-1].close,
                    volume=sum(bar.volume for bar in intraday.bars),
                )
            open_px = round(_daily_close(symbol.ticker, session_day) * 0.999, 2)
            return Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                start=start,
                end=end,
                open=open_px,
                high=open_px,
                low=open_px,
                close=open_px,
                volume=0,
            )
        close = _daily_close(symbol.ticker, session_day)
        row = _ohlc_from_close(symbol.ticker, start, close)
        row["start"] = start
        row["end"] = session_end
        row["volume"] = _volume(symbol.ticker, start, Timeframe.D1)
        return normalize_bar(row, symbol=symbol, timeframe=Timeframe.D1)

    def _intraday_bar(
        self,
        symbol: Symbol,
        timeframe: Timeframe,
        start: datetime,
        session_day: date,
    ) -> Bar:
        close = _daily_close(symbol.ticker, session_day)
        # Path around the daily close so bars are not identical.
        digest = _digest(f"path:{symbol.ticker}:{start.isoformat()}")
        step = ((int(digest[:4], 16) % 61) - 30) / 10_000
        px = round(close * (1 + step), 2)
        row = _ohlc_from_close(symbol.ticker, start, px)
        row["start"] = start
        row["end"] = start + bar_duration(timeframe)
        row["volume"] = _volume(symbol.ticker, start, timeframe)
        return normalize_bar(row, symbol=symbol, timeframe=timeframe)


def expected_full_session_count(timeframe: Timeframe) -> int:
    sample = date(2026, 9, 21)
    return len(
        expected_starts(
            sample,
            timeframe,
            session_open=time(9, 15),
            session_close=time(15, 30),
        )
    )
