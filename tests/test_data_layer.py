from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.data import (
    DataHub,
    LicensedFeed,
    NIFTY50_EQUITIES,
    NIFTY_INDICES,
    Timeframe,
    assert_research_payload,
    data_universe,
)
from grow.data.actions import CorporateAction, IdentityAdjuster, LicensedActions
from grow.data.boundary import research_view
from grow.data.fixture import expected_full_session_count
from grow.data.normalizer import normalize_bar
from grow.data.schedule import complete_starts, expected_starts
from grow.data.universe import SELECTED_NSE_STOCKS, is_in_data_universe
from grow.errors import GrowConfigError, GrowInterfaceNotImplemented, GrowSafetyError
from grow.strategies import StrategyBook, StrategySignal
from grow.types import Symbol

ROOT = Path(__file__).resolve().parents[1]


class UniverseTests(unittest.TestCase):
    def test_nifty50_freeze_is_fifty_names(self) -> None:
        self.assertEqual(len(NIFTY50_EQUITIES), 50)
        self.assertEqual(len(set(NIFTY50_EQUITIES)), 50)
        self.assertIn("RELIANCE", NIFTY50_EQUITIES)
        self.assertTrue(all(name == name.upper() for name in NIFTY50_EQUITIES))

    def test_indices_and_selected_stocks(self) -> None:
        self.assertIn("NIFTY", NIFTY_INDICES)
        self.assertIn("BANKNIFTY", NIFTY_INDICES)
        for ticker in SELECTED_NSE_STOCKS:
            self.assertTrue(is_in_data_universe(ticker))
        universe = {s.ticker for s in data_universe()}
        self.assertIn("NIFTY", universe)
        self.assertIn("TCS", universe)
        self.assertEqual(len(universe), len(NIFTY50_EQUITIES) + len(NIFTY_INDICES))


class ScheduleTests(unittest.TestCase):
    def test_full_session_bar_counts(self) -> None:
        self.assertEqual(expected_full_session_count(Timeframe.M15), 25)
        self.assertEqual(expected_full_session_count(Timeframe.M5), 75)
        self.assertEqual(expected_full_session_count(Timeframe.D1), 1)

    def test_complete_bars_at_1100(self) -> None:
        as_of = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
        from datetime import date, time

        m15 = complete_starts(
            date(2026, 9, 21),
            Timeframe.M15,
            as_of,
            session_open=time(9, 15),
            session_close=time(15, 30),
        )
        m5 = complete_starts(
            date(2026, 9, 21),
            Timeframe.M5,
            as_of,
            session_open=time(9, 15),
            session_close=time(15, 30),
        )
        self.assertEqual(len(m15), 7)
        self.assertEqual(len(m5), 21)


class FixtureHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(datetime(2026, 9, 21, 11, 0, tzinfo=IST))
        self.config = load_config()
        self.hub = DataHub(self.config, clock=self.clock)

    def test_snapshot_has_volume_and_source_metadata(self) -> None:
        snap = self.hub.snapshot("RELIANCE")
        self.assertEqual(snap.source.name, "grow.data.fixture.v1")
        self.assertTrue(snap.source.is_fixture)
        self.assertFalse(snap.source.is_live)
        self.assertEqual(snap.source.license, "synthetic-not-licensed")
        m15 = snap.series[Timeframe.M15]
        self.assertEqual(len(m15.bars), 7)
        self.assertTrue(all(bar.volume > 0 for bar in m15.bars))
        self.assertEqual(len(snap.series[Timeframe.M5].bars), 21)
        self.assertEqual(len(snap.series[Timeframe.D1].bars), self.config.data.history_sessions)
        self.assertGreater(snap.last_price, 0)

    def test_full_session_after_close(self) -> None:
        clock = FrozenClock(datetime(2026, 9, 21, 15, 30, tzinfo=IST))
        hub = DataHub(self.config, clock=clock)
        snap = hub.snapshot("NIFTY")
        self.assertEqual(len(snap.series[Timeframe.M15].bars), 25)
        self.assertEqual(len(snap.series[Timeframe.M5].bars), 75)

    def test_missing_candle_is_detected(self) -> None:
        from grow.data.quality import assess_series

        snap = self.hub.snapshot("INFY")
        series = snap.series[Timeframe.M15]
        truncated = type(series)(symbol=series.symbol, timeframe=series.timeframe, bars=series.bars[1:])
        quality = assess_series(
            truncated,
            session_day=datetime(2026, 9, 21).date(),
            as_of=self.clock.now(),
            session_open=self.hub.source.calendar.open_time,
            session_close=self.hub.source.calendar.close_time,
            stale_after_seconds=900,
        )
        self.assertGreater(quality.missing_count, 0)
        self.assertFalse(quality.complete)

    def test_research_view_has_no_raw_bars(self) -> None:
        snap = self.hub.snapshot("TCS")
        view = research_view(snap)
        payload = view.to_dict()
        assert_research_payload(payload)
        self.assertNotIn("bars", payload)
        self.assertNotIn("series", payload)
        with self.assertRaises(GrowSafetyError):
            assert_research_payload({"symbol": "TCS", "bars": [1, 2, 3]})

    def test_hub_does_not_emit_trades(self) -> None:
        self.assertFalse(hasattr(self.hub, "propose"))
        self.assertFalse(hasattr(self.hub, "submit"))
        self.assertFalse(hasattr(self.hub, "place_order"))

    def test_unknown_symbol_rejected(self) -> None:
        with self.assertRaises(GrowConfigError):
            self.hub.snapshot("NOTAREAL")

    def test_option_chain_still_refused(self) -> None:
        with self.assertRaises(GrowInterfaceNotImplemented):
            self.hub.option_chain("NIFTY")

    def test_licensed_feed_refused(self) -> None:
        with self.assertRaises(GrowInterfaceNotImplemented):
            LicensedFeed()
        with self.assertRaises(GrowConfigError):
            load_config(environ={"GROW_DATA_PROVIDER": "nse"})
        with self.assertRaises(GrowConfigError):
            load_config(environ={"GROW_ALLOW_LIVE_FEED": "true"})

    def test_corporate_actions_identity_only(self) -> None:
        snap = self.hub.snapshot("HDFCBANK")
        series = snap.series[Timeframe.M15]
        same = IdentityAdjuster().apply(series, ())
        self.assertIs(same, series)
        with self.assertRaises(GrowInterfaceNotImplemented):
            IdentityAdjuster().apply(
                series,
                (CorporateAction(symbol="HDFCBANK", ex_date=datetime(2026, 1, 1).date(), kind="split", factor=2.0),),
            )
        with self.assertRaises(GrowInterfaceNotImplemented):
            LicensedActions().load()

    def test_strategy_book_not_wired(self) -> None:
        with self.assertRaises(GrowInterfaceNotImplemented):
            StrategyBook().run("breakout")
        signal = StrategySignal(
            symbol=Symbol("RELIANCE"),
            strategy="breakout",
            direction="LONG",
            entry=1420.0,
            stop=1402.0,
            target=1455.0,
            confidence=0.72,
            timeframe=Timeframe.M15,
            reason="volume breakout + range expansion",
            as_of=self.clock.now(),
            snapshot_id="demo",
        )
        self.assertEqual(signal.to_dict()["direction"], "LONG")

    def test_normalizer_rejects_bad_ohlc(self) -> None:
        from datetime import time as dtime
        from grow.data.schedule import bar_duration

        start = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
        with self.assertRaises(GrowConfigError):
            normalize_bar(
                {
                    "start": start,
                    "end": start + bar_duration(Timeframe.M15),
                    "open": 100,
                    "high": 90,
                    "low": 80,
                    "close": 95,
                    "volume": 10,
                },
                symbol=Symbol("RELIANCE"),
                timeframe=Timeframe.M15,
            )
        with self.assertRaises(GrowConfigError):
            normalize_bar(
                {"start": start, "open": 100, "high": 110, "low": 90, "close": 105, "volume": -1},
                symbol=Symbol("RELIANCE"),
                timeframe=Timeframe.M15,
            )


class DataHygieneTests(unittest.TestCase):
    def test_no_scrape_or_vendor_clients(self) -> None:
        forbidden = (
            "import requests",
            "import httpx",
            "import yfinance",
            "import nsepython",
            "import nsetools",
            "from bs4",
            "import bs4",
            "urlopen",
            "kiteconnect",
            "websocket",
        )
        hits: list[str] = []
        for path in (ROOT / "grow" / "data").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            lower = text.lower()
            for token in forbidden:
                if token in lower:
                    hits.append(f"{path.name}: {token}")
        self.assertEqual(hits, [])

    def test_default_config_is_fixture(self) -> None:
        config = load_config()
        self.assertEqual(config.data.provider, "fixture")
        self.assertFalse(config.data.allow_live_feed)
        self.assertFalse(config.data.allow_options_chain)
        self.assertEqual(config.data.timeframes, ("D1", "M15", "M5"))


if __name__ == "__main__":
    unittest.main()
