"""Square-off window and flatten-path tests."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime

from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.errors import GrowSafetyError
from grow.market.research import MarketResearch
from grow.paper.ledger import PaperLedger
from grow.types import Intent, Regime, Side, Symbol, TradeProposal, Venue

from tests.helpers import make_guard


class SquareOffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.open_clock = FrozenClock(datetime(2026, 9, 21, 11, 0, tzinfo=IST))
        self.config = load_config()
        self.guard = make_guard(self.config, clock=self.open_clock)
        self.ledger = PaperLedger(self.config, self.guard, clock=self.open_clock)
        brief = MarketResearch(self.config, clock=self.open_clock).research("RELIANCE")
        object.__setattr__(brief, "regime", Regime.RANGING)
        self.brief = brief

    def _eval(self, proposal: TradeProposal, clock: FrozenClock | None = None, **book):
        guard = self.guard if clock is None else make_guard(self.config, clock=clock)
        return guard.evaluate(
            proposal,
            self.brief,
            cash=book.get("cash", self.ledger.book.cash),
            gross_notional=book.get("gross_notional", self.ledger.book.gross_notional),
            daily_pnl=book.get("daily_pnl", self.ledger.book.realized_pnl),
            symbol_notional=book.get(
                "symbol_notional", self.ledger.book.symbol_notional(proposal.symbol.ticker)
            ),
        )

    def _open_long(self, qty: int = 10) -> TradeProposal:
        price = self.brief.last_price
        proposal = TradeProposal(
            proposal_id="open-reliance",
            symbol=Symbol("RELIANCE"),
            side=Side.BUY,
            intent=Intent.OPEN,
            quantity=qty,
            limit_price=price,
            stop_loss=round(price * 0.98, 2),
            take_profit=round(price * 1.03, 2),
            thesis="test open",
            confidence=0.5,
            venue=Venue.PAPER,
            created_at=self.open_clock.now(),
            notional=round(qty * price, 2),
        )
        verdict = self._eval(proposal)
        self.assertTrue(verdict.approved, verdict.reason)
        self.ledger.submit(proposal, verdict.stamp)
        return proposal

    def test_square_off_unknown_position_returns_none(self) -> None:
        self.assertIsNone(self.ledger.square_off_proposal("RELIANCE", self.brief.last_price))

    def test_square_off_unknown_position_submit_rejected(self) -> None:
        price = self.brief.last_price
        proposal = TradeProposal(
            proposal_id="ghost-sqoff",
            symbol=Symbol("RELIANCE"),
            side=Side.SELL,
            intent=Intent.SQUARE_OFF,
            quantity=1,
            limit_price=price,
            stop_loss=None,
            take_profit=None,
            thesis="no position",
            confidence=1.0,
            venue=Venue.PAPER,
            created_at=self.open_clock.now(),
            notional=round(price, 2),
        )
        verdict = self._eval(proposal)
        self.assertTrue(verdict.approved, verdict.reason)
        with self.assertRaises(GrowSafetyError):
            self.ledger.submit(proposal, verdict.stamp)

    def test_square_off_quantity_exceeds_position(self) -> None:
        self._open_long(qty=10)
        price = self.brief.last_price
        built = self.ledger.square_off_proposal("RELIANCE", price)
        assert built is not None
        oversized = replace(built, quantity=11, notional=round(11 * price, 2), proposal_id="sqoff-oversize")
        verdict = self._eval(oversized)
        self.assertTrue(verdict.approved, verdict.reason)
        with self.assertRaises(GrowSafetyError):
            self.ledger.submit(oversized, verdict.stamp)

    def test_square_off_during_open_session_is_allowed(self) -> None:
        """Flatten is legal any time the cash session is open, not only after 15:15."""
        self._open_long(qty=4)
        proposal = self.ledger.square_off_proposal("RELIANCE", self.brief.last_price)
        assert proposal is not None
        verdict = self._eval(proposal)
        self.assertTrue(verdict.approved, verdict.reason)
        fill = self.ledger.submit(proposal, verdict.stamp)
        self.assertEqual(fill.quantity, 4)
        self.assertNotIn("RELIANCE", self.ledger.book.positions)

    def test_square_off_at_1529_allowed(self) -> None:
        self._open_long(qty=2)
        late = FrozenClock(datetime(2026, 9, 21, 15, 29, tzinfo=IST))
        proposal = self.ledger.square_off_proposal("RELIANCE", self.brief.last_price)
        assert proposal is not None
        verdict = self._eval(proposal, clock=late)
        self.assertTrue(verdict.approved, verdict.reason)
        flatten_rule = [r for r in verdict.rule_results if r[0] == "session.flatten"][0]
        self.assertTrue(flatten_rule[1])
        self.assertIn("SQUARE_OFF_WINDOW", flatten_rule[2])
        fill = self.ledger.submit(proposal, verdict.stamp)
        self.assertEqual(fill.side, Side.SELL)

    def test_square_off_at_1530_rejected(self) -> None:
        self._open_long(qty=2)
        closed = FrozenClock(datetime(2026, 9, 21, 15, 30, tzinfo=IST))
        proposal = self.ledger.square_off_proposal("RELIANCE", self.brief.last_price)
        assert proposal is not None
        verdict = self._eval(proposal, clock=closed)
        self.assertFalse(verdict.approved)
        self.assertIn("session.flatten", verdict.reason)
        self.assertIn("CLOSED", verdict.reason)

    def test_square_off_after_market_close_rejected(self) -> None:
        self._open_long(qty=2)
        night = FrozenClock(datetime(2026, 9, 21, 16, 5, tzinfo=IST))
        proposal = self.ledger.square_off_proposal("RELIANCE", self.brief.last_price)
        assert proposal is not None
        verdict = self._eval(proposal, clock=night)
        self.assertFalse(verdict.approved)
        self.assertIn("session.flatten", verdict.reason)

    def test_square_off_window_still_blocks_new_entries(self) -> None:
        late = FrozenClock(datetime(2026, 9, 21, 15, 20, tzinfo=IST))
        price = self.brief.last_price
        opening = TradeProposal(
            proposal_id="late-open",
            symbol=Symbol("RELIANCE"),
            side=Side.BUY,
            intent=Intent.OPEN,
            quantity=1,
            limit_price=price,
            stop_loss=round(price * 0.98, 2),
            take_profit=round(price * 1.03, 2),
            thesis="too late",
            confidence=0.4,
            venue=Venue.PAPER,
            created_at=late.now(),
            notional=round(price, 2),
        )
        verdict = self._eval(opening, clock=late)
        self.assertFalse(verdict.approved)
        self.assertIn("session.entries", verdict.reason)
