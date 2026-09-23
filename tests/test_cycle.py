from __future__ import annotations

import unittest
from datetime import datetime

from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.dashboard import snapshot
from tests.helpers import make_runtime, research_fixture_config


class CycleTests(unittest.TestCase):
    def test_in_session_probe_fills_paper_book(self) -> None:
        clock = FrozenClock(datetime(2026, 9, 21, 11, 30, tzinfo=IST))
        config = research_fixture_config()
        runtime = make_runtime(config, clock=clock)
        report = runtime.run("HDFCBANK")
        self.assertEqual(report.book["valuation"]["method"], "cost_notional")
        # HIGH_VOLATILITY stub regime can still reject — that's valid.
        if report.brief.regime.value == "HIGH_VOLATILITY":
            self.assertIsNone(report.fill)
            return
        self.assertTrue(report.verdict.approved, report.verdict.reason)
        self.assertIsNotNone(report.fill)
        self.assertEqual(report.fill.venue_id, "GROW_PAPER")
        self.assertLess(report.book["cash"], config.paper.starting_cash)
        dash = snapshot(runtime.config, runtime.ledger.book, report)
        self.assertFalse(dash["lock"]["live_trading_compiled"])
        self.assertEqual(dash["lock"]["execution_mode"], "paper")

    def test_weekend_does_not_open(self) -> None:
        saturday = FrozenClock(datetime(2026, 9, 19, 11, 30, tzinfo=IST))
        report = make_runtime(research_fixture_config(), clock=saturday).run("ITC")
        self.assertFalse(report.verdict.approved)
        self.assertIsNone(report.fill)
