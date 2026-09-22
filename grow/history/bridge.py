"""2G → 2A/2C snapshot adapters. Provider-neutral. No broker."""

from __future__ import annotations

import hashlib
from datetime import date, datetime

from grow.clock import IST
from grow.config import GrowConfig, load_config
from grow.data.quality import combine_quality
from grow.data.schedule import complete_starts
from grow.data.schema import Bar, BarSeries, MarketSnapshot, SnapshotQuality, SourceMeta, Timeframe
from grow.errors import GrowConfigError
from grow.history.calendar import session_state_at
from grow.history.models import HistoricalBar
from grow.history.store import CanonicalStore
from grow.options.models import ExpiryClass, FieldSource, OptionChainSnapshot, OptionContract, OptionExpiry, OptionType
from grow.types import Symbol

DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bar(item: HistoricalBar, symbol: Symbol) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe=Timeframe(item.timeframe),
        start=item.timestamp,
        end=item.end,
        open=item.open,
        high=item.high,
        low=item.low,
        close=item.close,
        volume=item.volume,
    )


class HistoricalMarketSource:
    def __init__(self, store: CanonicalStore, config: GrowConfig | None = None) -> None:
        self.store = store
        self.config = config or load_config()

    def meta(self) -> SourceMeta:
        return SourceMeta(
            name=self.store.meta.dataset_id,
            vendor=self.store.meta.provider_name,
            license=self.store.meta.license_status,
            is_live=False,
            is_fixture=self.store.meta.is_fixture,
            schema=self.store.meta.schema_version,
        )

    def snapshot(self, ticker: str, as_of: datetime | None = None) -> MarketSnapshot:
        if as_of is None:
            raise GrowConfigError("HISTORICAL_REQUIRES_AS_OF")
        moment = as_of.astimezone(IST)
        try:
            state = session_state_at(self.store, moment)
        except GrowConfigError as exc:
            if "CALENDAR_MISSING" in str(exc):
                raise GrowConfigError("DATA_UNAVAILABLE:CALENDAR_MISSING") from exc
            raise
        symbol = Symbol(ticker=ticker, exchange=self.config.market.exchange)
        series: dict[Timeframe, BarSeries] = {}
        for name in self.config.data.timeframes:
            tf = Timeframe(name)
            rows = self.store.bars_at(ticker, moment, timeframe=tf.value)
            bars = [_bar(item, symbol) for item in rows]
            if tf is Timeframe.D1:
                bars = self._forming_d1(symbol, moment, bars)
            series[tf] = BarSeries(symbol=symbol, timeframe=tf, bars=tuple(bars))
        last = 0.0
        if series.get(Timeframe.M15) and series[Timeframe.M15].bars:
            last = series[Timeframe.M15].bars[-1].close
        elif series.get(Timeframe.D1) and series[Timeframe.D1].bars:
            last = series[Timeframe.D1].bars[-1].close
        if last <= 0:
            raise GrowConfigError(DATA_UNAVAILABLE)
        session = self.store.session_on(moment.date())
        quality = self._bar_quality(series, moment, session)
        return MarketSnapshot(
            snapshot_id=_digest(f"{ticker}:{moment.isoformat()}:{self.store.meta.fingerprint}")[:16],
            symbol=symbol,
            as_of=moment,
            session=state,
            last_price=last,
            currency="INR",
            series=series,
            quality=quality,
            source=self.meta(),
        )

    def bars(self, ticker, timeframe, *, session_day, as_of=None, history_sessions=None):
        snap = self.snapshot(ticker, as_of=as_of)
        tf = timeframe if isinstance(timeframe, Timeframe) else Timeframe(str(timeframe).upper())
        return snap.series[tf]

    def _bar_quality(self, series: dict[Timeframe, BarSeries], as_of: datetime, session) -> SnapshotQuality:
        if session is None or session.status != "OPEN":
            last = None
            m15 = series.get(Timeframe.M15)
            if m15 and m15.bars:
                last = m15.bars[-1].end
            return SnapshotQuality(
                complete=True,
                stale=False,
                missing_count=0,
                expected_count=0,
                last_bar_end=last,
                notes=("session not open", self.store.meta.fingerprint[:12]),
            )
        parts: list[SnapshotQuality] = []
        open_t = session.open_at.time()
        close_t = session.close_at.time()
        for tf, item in series.items():
            if tf is Timeframe.D1:
                today = tuple(bar for bar in item.bars if bar.start.date() == as_of.date())
                missing = 0 if today else 1
                parts.append(
                    SnapshotQuality(
                        complete=missing == 0,
                        stale=False,
                        missing_count=missing,
                        expected_count=1,
                        last_bar_end=today[-1].end if today else None,
                        notes=("D1",),
                    )
                )
                continue
            expected = complete_starts(
                as_of.date(),
                tf,
                as_of,
                session_open=open_t,
                session_close=close_t,
            )
            have = {bar.start for bar in item.bars}
            miss = tuple(start for start in expected if start not in have)
            parts.append(
                SnapshotQuality(
                    complete=len(miss) == 0 and bool(expected),
                    stale=False,
                    missing_count=len(miss),
                    expected_count=len(expected),
                    last_bar_end=item.bars[-1].end if item.bars else None,
                    notes=(f"{tf.value}:missing={len(miss)}",),
                )
            )
        quality = combine_quality(tuple(parts))
        return SnapshotQuality(
            complete=quality.complete,
            stale=quality.stale,
            missing_count=quality.missing_count,
            expected_count=quality.expected_count,
            last_bar_end=quality.last_bar_end,
            notes=quality.notes + (self.store.meta.fingerprint[:12],),
        )

    def _forming_d1(self, symbol: Symbol, as_of: datetime, complete: list[Bar]) -> list[Bar]:
        session_open = as_of.replace(hour=9, minute=15, second=0, microsecond=0)
        close = as_of.replace(hour=15, minute=30, second=0, microsecond=0)
        kept = [bar for bar in complete if bar.end <= as_of]
        if as_of < close and as_of > session_open:
            m15 = self.store.bars_at(symbol.ticker, as_of, timeframe="M15")
            today = [b for b in m15 if b.timestamp.date() == as_of.date()]
            if today:
                forming = Bar(
                    symbol=symbol,
                    timeframe=Timeframe.D1,
                    start=session_open,
                    end=as_of,
                    open=today[0].open,
                    high=max(b.high for b in today),
                    low=min(b.low for b in today),
                    close=today[-1].close,
                    volume=sum(b.volume for b in today),
                )
                kept = [bar for bar in kept if bar.start.date() != as_of.date()]
                kept.append(forming)
        return kept


class HistoricalOptionSource:
    def __init__(self, store: CanonicalStore) -> None:
        self.store = store

    def meta(self) -> SourceMeta:
        return SourceMeta(
            name=self.store.meta.dataset_id,
            vendor=self.store.meta.provider_name,
            license=self.store.meta.license_status,
            is_live=False,
            is_fixture=self.store.meta.is_fixture,
            schema="grow.options.chain.v1",
        )

    def snapshot(self, underlying: str, as_of: datetime, *, spot: float) -> OptionChainSnapshot:
        contracts, quotes = self.store.snapshot_quotes(underlying, as_of.astimezone(IST))
        by_id = {c.contract_id: c for c in contracts}
        mapped: list[OptionContract] = []
        expiries: dict[date, OptionExpiry] = {}
        for quote in quotes:
            raw = by_id[quote.contract_id]
            if raw.expiry < as_of.date():
                continue
            klass = ExpiryClass.WEEKLY if raw.expiry_class == "WEEKLY" else ExpiryClass.MONTHLY
            mapped.append(
                OptionContract(
                    underlying=raw.underlying,
                    expiry=raw.expiry,
                    expiry_class=klass,
                    strike=raw.strike,
                    option_type=OptionType(raw.option_type),
                    bid=quote.bid,
                    ask=quote.ask,
                    last_price=quote.ltp,
                    volume=quote.volume,
                    open_interest=quote.open_interest,
                    previous_open_interest=quote.previous_open_interest,
                    implied_volatility=quote.implied_volatility,
                    delta=quote.delta,
                    gamma=quote.gamma,
                    theta=quote.theta,
                    vega=quote.vega,
                    timestamp=quote.timestamp,
                    provider_contract_id=raw.provider_contract_id,
                    iv_source=FieldSource.PROVIDER if quote.iv_source else FieldSource.UNAVAILABLE,
                    greek_source=FieldSource.PROVIDER if quote.greek_source else FieldSource.UNAVAILABLE,
                )
            )
            expiries[raw.expiry] = OptionExpiry(raw.expiry, klass)
        if not mapped:
            raise GrowConfigError(DATA_UNAVAILABLE)
        return OptionChainSnapshot(
            snapshot_id=_digest(f"{underlying}:{as_of.isoformat()}:{self.store.meta.fingerprint}")[:16],
            underlying=underlying,
            as_of=as_of.astimezone(IST),
            spot=spot,
            expiries=tuple(expiries.values()),
            contracts=tuple(mapped),
            source_id=self.store.meta.version,
            is_fixture=self.store.meta.is_fixture,
            provider_metadata={
                "dataset_id": self.store.meta.dataset_id,
                "dataset_version": self.store.meta.version,
                "fingerprint": self.store.meta.fingerprint,
                "calendar_version": self.store.meta.calendar_version,
            },
        )
