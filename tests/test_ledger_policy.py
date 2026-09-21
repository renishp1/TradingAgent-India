from __future__ import annotations

import unittest
from datetime import datetime

from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.errors import GrowSafetyError
from grow.paper.ledger import PaperBook, PaperLedger, Position
from grow.types import Intent, Side, Symbol, TradeProposal, Venue

from tests.helpers import make_guard


class LedgerPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(datetime(2026, 9, 21, 11, 0, tzinfo=IST))
        self.config = load_config()
        self.guard = make_guard(self.config, clock=self.clock)
        self.ledger = PaperLedger(self.config, self.guard, clock=self.clock)

    def test_position_cannot_be_negative(self) -> None:
        with self.assertRaises(GrowSafetyError):
            Position(symbol=Symbol("RELIANCE"), quantity=-10, average_price=100)

    def test_submit_open_sell_refused_even_without_waiting_for_stamp(self) -> None:
        p = TradeProposal(
            proposal_id="short-1",
            symbol=Symbol("RELIANCE"),
            side=Side.SELL,
            intent=Intent.OPEN,
            quantity=1,
            limit_price=100,
            stop_loss=102,
            take_profit=97,
            thesis="illegal short",
            confidence=0.5,
            venue=Venue.PAPER,
            created_at=self.clock.now(),
            notional=100,
        )
        with self.assertRaises(GrowSafetyError):
            self.ledger.submit(p, None)

    def test_snapshot_declares_cost_notional_not_mtm(self) -> None:
        snap = PaperBook(cash=1_000_000, currency="INR").snapshot()
        self.assertEqual(snap["valuation"]["method"], "cost_notional")
        self.assertIsNone(snap["valuation"]["unrealized_pnl"])
