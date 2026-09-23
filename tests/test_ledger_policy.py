from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime

from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.errors import GrowSafetyError
from grow.market.research import MarketResearch
from grow.paper.ledger import PaperBook, PaperLedger, Position
from grow.types import Intent, Regime, Side, Symbol, TradeProposal, Venue

from tests.helpers import make_guard, research_fixture_config


class LedgerPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(datetime(2026, 9, 21, 11, 0, tzinfo=IST))
        self.config = research_fixture_config()
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
        self.assertIsNone(snap["pnl"]["true_daily_pnl"])
        self.assertEqual(snap["pnl"]["fed_to_risk_guard_as"], "lifetime_realized_pnl")

    def test_notional_mismatch_rejected_without_stamp(self) -> None:
        p = TradeProposal(
            proposal_id="bad-notional",
            symbol=Symbol("RELIANCE"),
            side=Side.BUY,
            intent=Intent.OPEN,
            quantity=10,
            limit_price=100,
            stop_loss=98,
            take_profit=103,
            thesis="tampered notional",
            confidence=0.5,
            venue=Venue.PAPER,
            created_at=self.clock.now(),
            notional=1.0,
        )
        with self.assertRaises(GrowSafetyError):
            self.ledger.submit(p, None)

    def test_zero_quantity_rejected(self) -> None:
        p = TradeProposal(
            proposal_id="zero",
            symbol=Symbol("RELIANCE"),
            side=Side.BUY,
            intent=Intent.OPEN,
            quantity=0,
            limit_price=100,
            stop_loss=98,
            take_profit=103,
            thesis="zero",
            confidence=0.5,
            venue=Venue.PAPER,
            created_at=self.clock.now(),
            notional=0,
        )
        with self.assertRaises(GrowSafetyError):
            self.ledger.submit(p, None)

    def test_flatten_buy_rejected(self) -> None:
        p = TradeProposal(
            proposal_id="cover",
            symbol=Symbol("RELIANCE"),
            side=Side.BUY,
            intent=Intent.SQUARE_OFF,
            quantity=1,
            limit_price=100,
            stop_loss=None,
            take_profit=None,
            thesis="cover a short that must not exist",
            confidence=1.0,
            venue=Venue.PAPER,
            created_at=self.clock.now(),
            notional=100,
        )
        with self.assertRaises(GrowSafetyError):
            self.ledger.submit(p, None)

    def test_stamped_then_mutated_stop_does_not_fill(self) -> None:
        brief = MarketResearch(self.config, clock=self.clock).research("TCS")
        object.__setattr__(brief, "regime", Regime.RANGING)
        price = brief.last_price
        original = TradeProposal(
            proposal_id="bind-sl",
            symbol=Symbol("TCS"),
            side=Side.BUY,
            intent=Intent.OPEN,
            quantity=1,
            limit_price=price,
            stop_loss=round(price * 0.98, 2),
            take_profit=round(price * 1.03, 2),
            thesis="original",
            confidence=0.4,
            venue=Venue.PAPER,
            created_at=self.clock.now(),
            notional=round(price, 2),
        )
        verdict = self.guard.evaluate(
            original,
            brief,
            cash=1_000_000,
            gross_notional=0,
            daily_pnl=0,
            symbol_notional=0,
        )
        self.assertTrue(verdict.approved, verdict.reason)
        mutated = replace(original, stop_loss=1.0)
        with self.assertRaises(GrowSafetyError):
            self.ledger.submit(mutated, verdict.stamp)
        self.assertEqual(self.ledger.book.fills, [])
