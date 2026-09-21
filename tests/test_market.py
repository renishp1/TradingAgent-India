from __future__ import annotations

import unittest
from datetime import datetime

from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.errors import GrowInterfaceNotImplemented
from grow.market import MarketResearch, SessionCalendar
from grow.types import SessionState


class MarketTests(unittest.TestCase):
    def test_listed_and_stub_quote(self) -> None:
        clock = FrozenClock(datetime(2026, 9, 21, 10, 0, tzinfo=IST))
        research = MarketResearch(load_config(), clock=clock)
        self.assertTrue(research.is_listed("reliance"))
        self.assertFalse(research.is_listed("XYZABC"))
        brief = research.research("RELIANCE")
        self.assertEqual(brief.symbol.exchange, "NSE")
        self.assertGreater(brief.last_price, 0)
        self.assertIn("Stub quote", brief.notes[0])
        again = research.research("RELIANCE")
        self.assertEqual(brief.last_price, again.last_price)

    def test_session_states(self) -> None:
        config = load_config()
        cal = SessionCalendar(config.market, clock=FrozenClock(datetime(2026, 9, 21, 8, 0, tzinfo=IST)))
        self.assertEqual(cal.state(), SessionState.PRE_OPEN)
        cal = SessionCalendar(config.market, clock=FrozenClock(datetime(2026, 9, 21, 12, 0, tzinfo=IST)))
        self.assertEqual(cal.state(), SessionState.OPEN)
        cal = SessionCalendar(config.market, clock=FrozenClock(datetime(2026, 9, 21, 15, 20, tzinfo=IST)))
        self.assertEqual(cal.state(), SessionState.SQUARE_OFF_WINDOW)
        cal = SessionCalendar(config.market, clock=FrozenClock(datetime(2026, 9, 21, 16, 0, tzinfo=IST)))
        self.assertEqual(cal.state(), SessionState.CLOSED)
        cal = SessionCalendar(config.market, clock=FrozenClock(datetime(2026, 9, 19, 12, 0, tzinfo=IST)))
        self.assertEqual(cal.state(), SessionState.WEEKEND)
        cal = SessionCalendar(config.market, clock=FrozenClock(datetime(2026, 1, 26, 12, 0, tzinfo=IST)))
        self.assertEqual(cal.state(), SessionState.HOLIDAY)

    def test_deferred_desks_raise(self) -> None:
        from grow.data import DataHub
        from grow.learning import LearningStore
        from grow.options import OptionsDesk
        from grow.strategies import StrategyBook

        with self.assertRaises(GrowInterfaceNotImplemented):
            OptionsDesk().chain("NIFTY")
        with self.assertRaises(GrowInterfaceNotImplemented):
            StrategyBook().run("mean-reversion")
        with self.assertRaises(GrowInterfaceNotImplemented):
            DataHub().quote("RELIANCE")
        with self.assertRaises(GrowInterfaceNotImplemented):
            LearningStore().remember("x")
