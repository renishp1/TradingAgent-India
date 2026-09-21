from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.errors import GrowConfigError, GrowSafetyError
from grow.market.research import MarketResearch
from grow.paper.ledger import PaperLedger
from grow.risk.guard import RiskGuard
from grow.risk.secret import resolve_risk_secret
from grow.risk.stamp import STAMP_VERSION, canonical_proposal_json, stamp_token
from grow.types import Intent, Regime, RiskStamp, Side, Symbol, TradeProposal, Venue

from tests.helpers import TEST_RISK_SECRET, make_guard

ROOT = Path(__file__).resolve().parents[1]


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
        self.clock = FrozenClock(datetime(2026, 9, 21, 11, 0, tzinfo=IST))
        self.config = load_config()
        self.guard = make_guard(self.config, clock=self.clock)
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
            _proposal(
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
        guard = make_guard(self.config, clock=night)
        p = _proposal(limit_price=self.brief.last_price, notional=self.brief.last_price * 5, quantity=5)
        verdict = guard.evaluate(
            p, self.brief, cash=1_000_000, gross_notional=0, daily_pnl=0, symbol_notional=0
        )
        self.assertFalse(verdict.approved)
        self.assertIn("session", verdict.reason)

    def test_square_off_window_blocks_new_entries(self) -> None:
        late = FrozenClock(datetime(2026, 9, 21, 15, 20, tzinfo=IST))
        guard = make_guard(self.config, clock=late)
        p = _proposal(limit_price=self.brief.last_price, notional=self.brief.last_price, quantity=1)
        verdict = guard.evaluate(
            p, self.brief, cash=1_000_000, gross_notional=0, daily_pnl=0, symbol_notional=0
        )
        self.assertFalse(verdict.approved)
        self.assertIn("session.entries", verdict.reason)

    def test_square_off_window_allows_flatten(self) -> None:
        late = FrozenClock(datetime(2026, 9, 21, 15, 20, tzinfo=IST))
        guard = make_guard(self.config, clock=late)
        p = _proposal(
            intent=Intent.SQUARE_OFF,
            side=Side.SELL,
            stop_loss=None,
            limit_price=self.brief.last_price,
            quantity=1,
            notional=self.brief.last_price,
        )
        verdict = guard.evaluate(
            p, self.brief, cash=1_000_000, gross_notional=self.brief.last_price, daily_pnl=0, symbol_notional=self.brief.last_price
        )
        self.assertTrue(verdict.approved, verdict.reason)

    def test_unknown_symbol_rejected(self) -> None:
        p = _proposal(symbol=Symbol("NOTAREAL"), limit_price=100, notional=100, quantity=1)
        verdict = self._eval(p)
        self.assertFalse(verdict.approved)
        self.assertIn("universe", verdict.reason)

    def test_forged_stamp_rejected_by_ledger(self) -> None:
        ledger = PaperLedger(self.config, self.guard, clock=self.clock)
        p = _proposal(limit_price=self.brief.last_price, notional=self.brief.last_price, quantity=1)
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
        p = _proposal(limit_price=self.brief.last_price, notional=self.brief.last_price, quantity=1)
        verdict = self._eval(p, daily_pnl=-50_000)
        self.assertFalse(verdict.approved)
        self.assertIn("loss.daily", verdict.reason)

    def test_open_sell_rejected_as_short(self) -> None:
        p = _proposal(
            side=Side.SELL,
            intent=Intent.OPEN,
            limit_price=self.brief.last_price,
            stop_loss=round(self.brief.last_price * 1.02, 2),
            notional=self.brief.last_price,
            quantity=1,
        )
        verdict = self._eval(p)
        self.assertFalse(verdict.approved)
        self.assertIn("policy.long_only", verdict.reason)

    def test_concentration_reports_cost_notional_basis(self) -> None:
        verdict = self._eval(_proposal(limit_price=self.brief.last_price, notional=self.brief.last_price, quantity=1))
        conc = [r for r in verdict.rule_results if r[0] == "concentration.symbol"][0]
        self.assertIn("basis=cost_notional", conc[2])

    def test_stamp_binds_stop_take_and_notional(self) -> None:
        original = _proposal(limit_price=self.brief.last_price, notional=self.brief.last_price * 10)
        verdict = self._eval(original)
        self.assertTrue(verdict.approved, verdict.reason)
        self.assertTrue(self.guard.verify_stamp(original, verdict.stamp))
        payload = json.loads(canonical_proposal_json(original, self.config.risk.ruleset))
        self.assertEqual(payload["v"], STAMP_VERSION)
        self.assertEqual(payload["stop_loss"], f"{original.stop_loss:.4f}")
        self.assertEqual(payload["take_profit"], f"{original.take_profit:.4f}")
        self.assertEqual(payload["notional"], f"{original.notional:.4f}")
        self.assertFalse(self.guard.verify_stamp(replace(original, stop_loss=1.0), verdict.stamp))
        self.assertFalse(self.guard.verify_stamp(replace(original, take_profit=9_999.0), verdict.stamp))
        self.assertFalse(
            self.guard.verify_stamp(
                replace(original, notional=round(original.notional + 1, 2)),
                verdict.stamp,
            )
        )
        self.assertFalse(self.guard.verify_stamp(replace(original, quantity=original.quantity + 1), verdict.stamp))
        # Commentary is not a fill field; thesis mutation still verifies.
        self.assertTrue(self.guard.verify_stamp(replace(original, thesis="rewritten"), verdict.stamp))

    def test_notional_mismatch_fails_guard(self) -> None:
        p = _proposal(limit_price=self.brief.last_price, quantity=10, notional=1.0)
        verdict = self._eval(p)
        self.assertFalse(verdict.approved)
        self.assertIn("notional.matches", verdict.reason)

    def test_guard_token_equals_stamp_module(self) -> None:
        original = _proposal(limit_price=self.brief.last_price, notional=self.brief.last_price * 10)
        verdict = self._eval(original)
        self.assertTrue(verdict.approved, verdict.reason)
        expected = stamp_token(original, self.config.risk.ruleset, TEST_RISK_SECRET)
        self.assertEqual(verdict.stamp.token, expected)
        self.assertEqual(json.loads(canonical_proposal_json(original, self.config.risk.ruleset))["v"], STAMP_VERSION)


class RiskSecretTests(unittest.TestCase):
    def test_missing_secret_fails_closed(self) -> None:
        with self.assertRaises(GrowConfigError):
            resolve_risk_secret(explicit=None, environ={})
        with self.assertRaises(GrowConfigError):
            RiskGuard(load_config(), secret="")

    def test_published_default_refused(self) -> None:
        with self.assertRaises(GrowConfigError):
            resolve_risk_secret(explicit="grow-risk-v1-paper-only")

    def test_source_has_no_default_secret_fallback(self) -> None:
        src = (ROOT / "grow" / "risk" / "guard.py").read_text(encoding="utf-8")
        self.assertNotIn("grow-risk-v1-paper-only", src)
        self.assertNotIn("_RULESET_SECRET", src)
        self.assertNotIn('os.environ.get("GROW_RISK_SECRET"', src)
        self.assertNotIn("hmac.new", src)
        self.assertNotIn("hashlib", src)
        self.assertIn("from grow.risk.stamp import stamp_token", src)
        self.assertIn("from grow.risk.secret import resolve_risk_secret", src)
        self.assertIn("stamp_token(proposal, self.config.risk.ruleset, self._secret)", src)

    def test_executable_tree_has_no_published_secret_fallback(self) -> None:
        """The old default may appear only as a refused value, never as a fallback."""
        forbidden_as_fallback = (
            '_RULESET_SECRET',
            'os.environ.get("GROW_RISK_SECRET", "grow-risk-v1-paper-only")',
            "os.environ.get('GROW_RISK_SECRET', 'grow-risk-v1-paper-only')",
        )
        for path in (ROOT / "grow").rglob("*.py"):
            src = path.read_text(encoding="utf-8")
            for needle in forbidden_as_fallback:
                self.assertNotIn(needle, src, f"{path.relative_to(ROOT)} still contains {needle}")
            if path.name == "secret.py":
                self.assertIn("grow-risk-v1-paper-only", src)
                continue
            self.assertNotIn("grow-risk-v1-paper-only", src, path)

    def test_injected_secret_stamps(self) -> None:
        secret = TEST_RISK_SECRET
        other = "grow-test-hmac-v2"
        clock = FrozenClock(datetime(2026, 9, 21, 11, 0, tzinfo=IST))
        config = load_config()
        a = RiskGuard(config, clock=clock, secret=secret)
        b = RiskGuard(config, clock=clock, secret=other)
        brief = MarketResearch(config, clock=clock).research("RELIANCE")
        object.__setattr__(brief, "regime", Regime.RANGING)
        p = _proposal(limit_price=brief.last_price, notional=brief.last_price, quantity=1)
        verdict = a.evaluate(p, brief, cash=1_000_000, gross_notional=0, daily_pnl=0, symbol_notional=0)
        self.assertTrue(verdict.approved, verdict.reason)
        self.assertFalse(b.verify_stamp(p, verdict.stamp))


if __name__ == "__main__":
    unittest.main()
