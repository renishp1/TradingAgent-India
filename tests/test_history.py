from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta

from grow.backtest.calendar import WeekdayFixtureCalendar
from grow.backtest.runner import BacktestRunner
from grow.clock import IST
from grow.config import load_config
from grow.director.catalog import default_catalog
from grow.errors import GrowConfigError
from grow.history.adapter import load_payload
from grow.history.bridge import HistoricalMarketSource, HistoricalOptionSource
from grow.history.calendar import calendar_for
from grow.history.models import (
    DatasetVersion,
    HistoricalBar,
    HistoricalOptionContract,
    HistoricalOptionQuote,
    HistoricalSession,
)
from grow.history.quality import validate_bar, validate_quote
from grow.history.registry import default_registry
from grow.history.sample import SAMPLE_ID, SAMPLE_VERSION, build_sample_store
from grow.history.store import CanonicalStore


def _meta(**kwargs) -> DatasetVersion:
    base = dict(
        dataset_id="test.hist",
        version="v1",
        source_version="s",
        normalization_version="history.normalize.v1",
        schema_version="grow.history.canonical.v1",
        calendar_version="cal.v1",
        coverage_start=date(2026, 9, 21),
        coverage_end=date(2026, 9, 21),
        fingerprint="pending",
        published_at="2026-01-01T08:00:00+05:30",
        quality_status="APPROVED",
        license_status="APPROVED",
        source_id="t",
        provider_name="t",
        granularity=("M15", "D1"),
        instrument_scope=("NIFTY",),
        bid_ask_available=True,
        oi_available=True,
        volume_available=True,
        iv_available=False,
        greeks_available=False,
        contract_metadata_available=True,
        option_depth="atm_pm2",
        provenance="test",
        timezone="Asia/Kolkata",
    )
    base.update(kwargs)
    return DatasetVersion(**base)


def _session(day=date(2026, 9, 21)) -> HistoricalSession:
    return HistoricalSession(
        session_date=day,
        open_at=datetime.combine(day, time(9, 15), tzinfo=IST),
        close_at=datetime.combine(day, time(15, 30), tzinfo=IST),
        source="t",
        calendar_version="cal.v1",
        status="OPEN",
    )


class HistoryQualityTests(unittest.TestCase):
    def test_naive_timestamp_rejected(self) -> None:
        bar = HistoricalBar(
            symbol="NIFTY",
            timeframe="M15",
            timestamp=datetime(2026, 9, 21, 11, 0),
            end=datetime(2026, 9, 21, 11, 15, tzinfo=IST),
            open=1,
            high=2,
            low=1,
            close=1,
            volume=1,
            source_id="t",
            dataset_version="v1",
            as_of_available_at=datetime(2026, 9, 21, 11, 15, tzinfo=IST),
            corporate_action_adjustment_version="u",
            quality_flags=(),
        )
        with self.assertRaises(GrowConfigError) as ctx:
            validate_bar(bar)
        self.assertIn("NAIVE_TIMESTAMP", str(ctx.exception))

    def test_invalid_ohlc_and_unsupported(self) -> None:
        ts = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
        end = ts + timedelta(minutes=15)
        bad = HistoricalBar(
            symbol="NIFTY",
            timeframe="M15",
            timestamp=ts,
            end=end,
            open=10,
            high=9,
            low=8,
            close=10,
            volume=1,
            source_id="t",
            dataset_version="v1",
            as_of_available_at=end,
            corporate_action_adjustment_version="u",
            quality_flags=(),
        )
        with self.assertRaises(GrowConfigError) as ctx:
            validate_bar(bad)
        self.assertIn("INVALID_OHLC", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            validate_bar(
                HistoricalBar(
                    symbol="RELIANCE",
                    timeframe="M15",
                    timestamp=ts,
                    end=end,
                    open=10,
                    high=11,
                    low=9,
                    close=10,
                    volume=1,
                    source_id="t",
                    dataset_version="v1",
                    as_of_available_at=end,
                    corporate_action_adjustment_version="u",
                    quality_flags=(),
                )
            )
        self.assertIn("UNSUPPORTED_UNDERLYING", str(ctx.exception))

    def test_crossed_and_negative_quote(self) -> None:
        ts = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
        with self.assertRaises(GrowConfigError) as ctx:
            validate_quote(
                HistoricalOptionQuote(
                    contract_id="x",
                    timestamp=ts,
                    bid=12,
                    ask=10,
                    ltp=11,
                    volume=1,
                    open_interest=1,
                    previous_open_interest=None,
                    implied_volatility=None,
                    delta=None,
                    gamma=None,
                    theta=None,
                    vega=None,
                    greek_source=None,
                    iv_source=None,
                    source_id="t",
                    dataset_version="v1",
                    as_of_available_at=ts,
                    quality_flags=(),
                )
            )
        self.assertIn("CROSSED_QUOTE", str(ctx.exception))


class HistoryStoreTests(unittest.TestCase):
    def test_duplicates_and_immutability(self) -> None:
        store = CanonicalStore(_meta())
        store.add_session(_session())
        ts = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
        end = ts + timedelta(minutes=15)
        bar = HistoricalBar(
            symbol="NIFTY",
            timeframe="M15",
            timestamp=ts,
            end=end,
            open=10,
            high=11,
            low=9,
            close=10,
            volume=1,
            source_id="t",
            dataset_version="v1",
            as_of_available_at=end,
            corporate_action_adjustment_version="u",
            quality_flags=(),
        )
        store.add_bar(bar)
        with self.assertRaises(GrowConfigError):
            store.add_bar(bar)
        first = datetime(2026, 9, 14, 9, 15, tzinfo=IST)
        last = datetime(2026, 9, 22, 15, 30, tzinfo=IST)
        contract = HistoricalOptionContract(
            underlying="NIFTY",
            expiry=date(2026, 9, 22),
            strike=25000,
            option_type="CE",
            contract_id="c1",
            provider_contract_id="p1",
            lot_size=75,
            expiry_class="WEEKLY",
            first_seen_at=first,
            last_seen_at=last,
            listing_status="ACTIVE",
            source_id="t",
            dataset_version="v1",
        )
        store.add_contract(contract)
        with self.assertRaises(GrowConfigError):
            store.add_contract(contract)
        quote = HistoricalOptionQuote(
            contract_id="c1",
            timestamp=ts,
            bid=10,
            ask=11,
            ltp=10.5,
            volume=1,
            open_interest=1,
            previous_open_interest=None,
            implied_volatility=None,
            delta=None,
            gamma=None,
            theta=None,
            vega=None,
            greek_source=None,
            iv_source=None,
            source_id="t",
            dataset_version="v1",
            as_of_available_at=ts,
            quality_flags=(),
        )
        store.add_quote(quote)
        with self.assertRaises(GrowConfigError):
            store.add_quote(quote)
        meta = store.publish()
        self.assertTrue(meta.fingerprint)
        with self.assertRaises(GrowConfigError) as ctx:
            store.add_bar(bar)
        self.assertIn("DATASET_IMMUTABLE", str(ctx.exception))
        self.assertEqual(store.lot_size("c1"), 75)

    def test_point_in_time_and_future_contract(self) -> None:
        store = CanonicalStore(_meta())
        store.add_session(_session())
        t0 = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
        t1 = datetime(2026, 9, 21, 12, 0, tzinfo=IST)
        first = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
        last = datetime(2026, 9, 22, 15, 30, tzinfo=IST)
        store.add_contract(
            HistoricalOptionContract(
                underlying="NIFTY",
                expiry=date(2026, 9, 22),
                strike=25000,
                option_type="CE",
                contract_id="now",
                provider_contract_id="now",
                lot_size=75,
                expiry_class="WEEKLY",
                first_seen_at=first,
                last_seen_at=last,
                listing_status="ACTIVE",
                source_id="t",
                dataset_version="v1",
            )
        )
        store.add_contract(
            HistoricalOptionContract(
                underlying="NIFTY",
                expiry=date(2026, 9, 29),
                strike=25000,
                option_type="CE",
                contract_id="future",
                provider_contract_id="future",
                lot_size=75,
                expiry_class="WEEKLY",
                first_seen_at=t1,
                last_seen_at=datetime(2026, 9, 29, 15, 30, tzinfo=IST),
                listing_status="ACTIVE",
                source_id="t",
                dataset_version="v1",
            )
        )
        store.add_quote(
            HistoricalOptionQuote(
                contract_id="now",
                timestamp=t0,
                bid=10,
                ask=11,
                ltp=10.5,
                volume=1,
                open_interest=1,
                previous_open_interest=None,
                implied_volatility=None,
                delta=None,
                gamma=None,
                theta=None,
                vega=None,
                greek_source=None,
                iv_source=None,
                source_id="t",
                dataset_version="v1",
                as_of_available_at=t0,
                quality_flags=(),
            )
        )
        before, q_before = store.snapshot_quotes("NIFTY", t0)
        store.add_quote(
            HistoricalOptionQuote(
                contract_id="now",
                timestamp=t1,
                bid=20,
                ask=21,
                ltp=20.5,
                volume=9,
                open_interest=9,
                previous_open_interest=None,
                implied_volatility=None,
                delta=None,
                gamma=None,
                theta=None,
                vega=None,
                greek_source=None,
                iv_source=None,
                source_id="t",
                dataset_version="v1",
                as_of_available_at=t1,
                quality_flags=(),
            )
        )
        after, q_after = store.snapshot_quotes("NIFTY", t0)
        self.assertEqual([c.contract_id for c in before], [c.contract_id for c in after])
        self.assertEqual([q.ltp for q in q_before], [q.ltp for q in q_after])
        self.assertNotIn("future", [c.contract_id for c in after])
        later, _ = store.snapshot_quotes("NIFTY", t1)
        self.assertIn("future", [c.contract_id for c in later])


class HistorySampleTests(unittest.TestCase):
    def test_sample_fingerprint_and_catalog(self) -> None:
        a = build_sample_store()
        b = build_sample_store()
        self.assertEqual(a.meta.fingerprint, b.meta.fingerprint)
        self.assertEqual(a.meta.dataset_id, SAMPLE_ID)
        self.assertEqual(a.meta.version, SAMPLE_VERSION)
        cat = default_catalog()
        self.assertIn(SAMPLE_ID, cat)
        self.assertEqual(cat[SAMPLE_ID].licensing_status, "APPROVED")
        self.assertIn(a.meta.fingerprint, cat[SAMPLE_ID].provenance)
        days = [s.session_date for s in a.sessions(date(2026, 9, 7), date(2026, 9, 9))]
        self.assertNotIn(date(2026, 9, 8), days)
        weekdays = WeekdayFixtureCalendar().sessions(date(2026, 9, 7), date(2026, 9, 9))
        self.assertIn(date(2026, 9, 8), weekdays)

    def test_no_lookahead_on_sample_snapshot(self) -> None:
        store = default_registry().get(SAMPLE_ID, SAMPLE_VERSION)
        as_of = datetime(2026, 9, 15, 11, 0, tzinfo=IST)
        src = HistoricalMarketSource(store)
        snap = src.snapshot("NIFTY", as_of=as_of)
        self.assertEqual(snap.as_of, as_of)
        for series in snap.series.values():
            if series.bars:
                self.assertLessEqual(series.bars[-1].end, as_of)
        chain = HistoricalOptionSource(store).snapshot("NIFTY", as_of, spot=snap.last_price)
        self.assertEqual(chain.as_of, as_of)
        self.assertTrue(chain.contracts)
        for contract in chain.contracts:
            self.assertLessEqual(contract.timestamp, as_of)
        self.assertEqual(store.lot_size(next(iter(store._contracts))), 75)

    def test_2e_consumes_2g_and_manifest(self) -> None:
        store = default_registry().get(SAMPLE_ID, SAMPLE_VERSION)
        runner = BacktestRunner(
            load_config(),
            calendar=calendar_for(store, date(2026, 9, 15), date(2026, 9, 15)),
            market_source=HistoricalMarketSource(store),
            option_source=HistoricalOptionSource(store),
        )
        result = runner.run(start=date(2026, 9, 15), end=date(2026, 9, 15), underlyings=("NIFTY",))
        self.assertEqual(result.manifest.dataset_id, SAMPLE_ID)
        self.assertEqual(result.manifest.dataset_version, SAMPLE_VERSION)
        self.assertEqual(result.manifest.calendar_version, store.meta.calendar_version)
        self.assertTrue(result.coverage["complete"])
        self.assertIn(result.leakage_status, {"CLEAN", "LEAKAGE", "UNKNOWN"})

    def test_file_adapter_roundtrip_fingerprint(self) -> None:
        store = CanonicalStore(_meta(version="file-v1"))
        store.add_session(_session())
        payload = {
            "meta": {**store.meta.to_dict(), "version": "file-v1", "coverage_start": "2026-09-21", "coverage_end": "2026-09-21"},
            "sessions": [_session().to_dict()],
            "bars": [],
            "contracts": [],
            "quotes": [],
        }
        loaded = load_payload(payload)
        self.assertTrue(loaded.meta.fingerprint)
        again = load_payload(payload)
        self.assertEqual(loaded.meta.fingerprint, again.meta.fingerprint)


if __name__ == "__main__":
    unittest.main()
