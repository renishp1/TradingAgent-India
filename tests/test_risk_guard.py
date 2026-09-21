from __future__ import annotations

import unittest
from datetime import datetime
from uuid import uuid4

from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.errors import GrowSafetyError
from grow.market.research import MarketResearch
from grow.paper.ledger import PaperLedger
from grow.risk.guard import RiskGuard
from grow.types import Intent, Regime, RiskStamp, Side, Symbol, TradeProposal, Venue


def _proposal(**kwargs) -> TradeProposal:
    symbol = kwargs.pop("symbol", Symbol("RELIANCE"))
    price = kwargs.pop("limit_price", 1400.0)
    qty = kwargs.pop("quantity", 10)
    defaults = dict(
        proposal_id=f"t-{uuid4().hex[:8]}",
        symbol=symbol,
        side=Side.BUY,
        intent=Intent.OPEN,
        quantity=qty,
        limit_price=price,
        stop_loss=round(price * 0.98, 2),
        take_profit=round(price * 1.03, 2),
        thesis="test",
        confidence=0.5,
        venue=Venue.PAPER,
        created_at=datetime(2026, 9, 21, 11, 0, tzinfo=IST),
        notional=round(qty * price, 2),
    )
    defaults.update(kwargs)
    return TradeProposal(**defaults)


class RiskGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(datetime(2026, 9, 21, 11, 0, tzinfo=IST))  # Monday in session
        self.config = load_config()
        self.guard = RiskGuard(self.config, clock=self.clock)
        brief = MarketResearch(self.config, clock=self.clock).research("RELIANCE")
        object.__setattr__(brief, "regime", Regime.RANGING)
        self.brief = brief

    def _eval(self, proposal: TradeProposal, **book):
        return self.guard.evaluate(
            proposal,
            self.brief,
            cash=book.get("cash", 1_000_000),
            gross_notional=book.get("gross_notional", 0.0),
            daily_pnl=book.get("daily_pnl", 0.0),
            symbol_notional=book.get("symbol_notional", 0.0),
        )

    def test_happy_path_stamps(self) -> None:
        verdict = self._eval(_proposal(limit_price=self.brief.last_price, notional=self.brief.last_price * 10))
        self.assertTrue(verdict.approved, verdict.reason)
        self.assertIsNotNone(verdict.stamp)
        self.assertTrue(self.guard.verify_stamp(
            _proposal(  # different id — should fail
                proposal_id="other",
                limit_price=self.brief.last_price,
                notional=self.brief.last_price * 10,
            ),
            verdict.stamp,
        ) is False)

    def test_missing_stop_rejected(self) -> None:
        p = _proposal(limit_price=self.brief.last_price, stop_loss=None, notional=self.brief.last_price * 10)
        verdict = self._eval(p)
        self.assertFalse(verdict.approved)
        self.assertIn("stop", verdict.reason)

    def test_oversize_rejected(self) -> None:
        p = _proposal(quantity=1000, limit_price=self.brief.last_price, notional=1000 * self.brief.last_price)
        verdict = self._eval(p)
        self.assertFalse(verdict.approved)
        self.assertIn("notional", verdict.reason)

    def test_after_hours_open_rejected(self) -> None:
        night = FrozenClock(datetime(2026, 9, 21, 18, 0, tzinfo=IST))
        guard = RiskGuard(self.config, clock=night)
        p = _proposal(limit_price=self.brief.last_price, notional=self.brief.last_price * 5)
        verdict = guard.evaluate(
            p, self.brief, cash=1_000_000, gross_notional=0, daily_pnl=0, symbol_notional=0
        )
        self.assertFalse(verdict.approved)
        self.assertIn("session", verdict.reason)

    def test_square_off_window_blocks_new_entries(self) -> None:
        late = FrozenClock(datetime(2026, 9, 21, 15, 20, tzinfo=IST))
        guard = RiskGuard(self.config, clock=late)
        p = _proposal(limit_price=self.brief.last_price, notional=self.brief.last_price)
        verdict = guard.evaluate(
            p, self.brief, cash=1_000_000, gross_notional=0, daily_pnl=0, symbol_notional=0
        )
        self.assertFalse(verdict.approved)
        self.assertIn("session.entries", verdict.reason)

    def test_square_off_window_allows_flatten(self) -> None:
        late = FrozenClock(datetime(2026, 9, 21, 15, 20, tzinfo=IST))
        guard = RiskGuard(self.config, clock=late)
        p = _proposal(
            intent=Intent.SQUARE_OFF,
            side=Side.SELL,
            stop_loss=None,
            limit_price=self.brief.last_price,
            notional=self.brief.last_price,
        )
        verdict = guard.evaluate(
            p, self.brief, cash=1_000_000, gross_notional=self.brief.last_price, daily_pnl=0, symbol_notional=self.brief.last_price
        )
        self.assertTrue(verdict.approved, verdict.reason)

    def test_unknown_symbol_rejected(self) -> None:
        p = _proposal(symbol=Symbol("NOTAREAL"), limit_price=100, notional=100)
        verdict = self._eval(p)
        self.assertFalse(verdict.approved)
        self.assertIn("universe", verdict.reason)

    def test_forged_stamp_rejected_by_ledger(self) -> None:
        ledger = PaperLedger(self.config, self.guard, clock=self.clock)
        p = _proposal(limit_price=self.brief.last_price, notional=self.brief.last_price)
        fake = RiskStamp(
            proposal_id=p.proposal_id,
            issued_at=self.clock.now(),
            ruleset=self.config.risk.ruleset,
            token="0" * 64,
        )
        with self.assertRaises(GrowSafetyError):
            ledger.submit(p, fake)
        with self.assertRaises(GrowSafetyError):
            ledger.submit(p, None)

    def test_daily_loss_halt(self) -> None:
        p = _proposal(limit_price=self.brief.last_price, notional=self.brief.last_price)
        verdict = self._eval(p, daily_pnl=-50_000)
        self.assertFalse(verdict.approved)
        self.assertIn("loss.daily", verdict.reason)


if __name__ == "__main__":
    unittest.main()
