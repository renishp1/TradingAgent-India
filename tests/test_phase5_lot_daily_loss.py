"""Phase 5 — fail-closed lot/lots rules and trading-day daily loss."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from grow.clock import IST

from tests.test_paper_execution import (
    AS_OF,
    _approved,
    _config,
    _engine,
    _metrics,
    _quote,
    _snapshot,
)


class Phase5LotRulesTests(unittest.TestCase):
    def test_missing_lots_rejects(self) -> None:
        config = _config()
        engine, clock = _engine(config)
        snap = _snapshot()
        metrics = {
            "strategy": "trend",
            "direction": "BULLISH",
            "underlying": "RELIANCE",
            "limit_price": 100.0,
            "stop_loss": 80.0,
            "quantity": 1,
        }
        decision, package = _approved(config, clock, snap, cycle_id="cycle-nolots", metrics=metrics)
        result = engine.execute(decision, snap, package=package)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "MISSING_LOTS")
        self.assertEqual(engine.positions.open_positions(), ())

    def test_decision_lot_size_never_fills_missing_quote_lot(self) -> None:
        config = _config()
        engine, clock = _engine(config)
        snap = _snapshot(quotes=(_quote(AS_OF, lot_size=None),))
        decision, package = _approved(
            config,
            clock,
            snap,
            cycle_id="cycle-nofallback",
            metrics=_metrics(lot_size=75, lots=1, quantity=75),
        )
        result = engine.execute(decision, snap, package=package)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "MISSING_LOT_SIZE")
        self.assertEqual(engine.positions.open_positions(), ())

    def test_lot_size_mismatch_still_rejects(self) -> None:
        config = _config()
        engine, clock = _engine(config)
        snap = _snapshot(quotes=(_quote(AS_OF, lot_size=50),))
        decision, package = _approved(
            config,
            clock,
            snap,
            cycle_id="cycle-mismatch",
            metrics=_metrics(lot_size=75, lots=1, quantity=75),
        )
        result = engine.execute(decision, snap, package=package)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "LOT_SIZE_MISMATCH")


class Phase5TradingDayLossTests(unittest.TestCase):
    def test_prior_day_loss_does_not_block_next_trading_day(self) -> None:
        config = _config(max_per_trade_risk=1000, max_daily_loss=10, max_open_positions=2)
        config = replace(
            config,
            live_data=replace(config.live_data, session_timeout_seconds=7 * 86400),
        )
        engine, clock = _engine(config)
        day1 = AS_OF
        snap1 = _snapshot(day1)
        decision_a, package_a = _approved(config, clock, snap1, cycle_id="cycle-day1-a")
        opened = engine.execute(decision_a, snap1, package=package_a)
        self.assertTrue(opened.accepted, opened.reason)

        stop_at = day1 + timedelta(minutes=5)
        clock.advance(timedelta(minutes=5))
        stopped = engine.on_snapshot(_snapshot(stop_at, quotes=(_quote(stop_at, ltp=70.0, bid=70.0, ask=71.0),)))
        self.assertIn("STOP_LOSS", stopped)
        day1_pnl = engine.positions.trading_day_realized_pnl(stop_at)
        self.assertLess(day1_pnl, -10)

        snap_blocked = _snapshot(
            stop_at,
            quotes=(
                _quote(stop_at),
                _quote(stop_at, strike=2600.0, provider_contract_id="RELIANCE-2600-CE"),
            ),
        )
        blocked_decision, blocked_package = _approved(
            config,
            clock,
            snap_blocked,
            instrument="RELIANCE-2600-CE",
            cycle_id="cycle-day1-b",
        )
        blocked = engine.execute(blocked_decision, snap_blocked, package=blocked_package)
        self.assertFalse(blocked.accepted)
        self.assertIn("loss.daily", blocked.reason)

        day2 = datetime(2026, 9, 23, 11, 0, tzinfo=IST)
        clock.advance(day2 - clock.now())
        snap2 = _snapshot(
            day2,
            quotes=(_quote(day2, strike=2600.0, provider_contract_id="RELIANCE-2600-CE"),),
        )
        decision_b, package_b = _approved(
            config,
            clock,
            snap2,
            instrument="RELIANCE-2600-CE",
            cycle_id="cycle-day2",
        )
        reopened = engine.execute(decision_b, snap2, package=package_b)
        self.assertTrue(reopened.accepted, reopened.reason)
        self.assertEqual(engine.positions.trading_day_realized_pnl(day2), 0.0)
        self.assertEqual(engine.last_risk_daily_pnl, 0.0)
        self.assertEqual(len(engine.positions.open_positions()), 1)
        self.assertFalse(engine._daily_loss_halt)

    def test_same_day_loss_still_blocks(self) -> None:
        config = _config(max_per_trade_risk=1000, max_daily_loss=10, max_open_positions=2)
        engine, clock = _engine(config)
        snap = _snapshot()
        decision_a, package_a = _approved(config, clock, snap, cycle_id="cycle-same-a")
        self.assertTrue(engine.execute(decision_a, snap, package=package_a).accepted)
        stop_at = AS_OF + timedelta(minutes=5)
        clock.advance(timedelta(minutes=5))
        engine.on_snapshot(_snapshot(stop_at, quotes=(_quote(stop_at, ltp=70.0, bid=70.0, ask=71.0),)))
        snap_b = _snapshot(
            stop_at,
            quotes=(
                _quote(stop_at),
                _quote(stop_at, strike=2600.0, provider_contract_id="RELIANCE-2600-CE"),
            ),
        )
        decision_b, package_b = _approved(
            config,
            clock,
            snap_b,
            instrument="RELIANCE-2600-CE",
            cycle_id="cycle-same-b",
        )
        blocked = engine.execute(decision_b, snap_b, package=package_b)
        self.assertFalse(blocked.accepted)
        self.assertIn("loss.daily", blocked.reason)
        lifetime = engine.positions.summary().net_realized_pnl
        self.assertEqual(engine.last_risk_daily_pnl, engine.positions.trading_day_realized_pnl(stop_at))
        self.assertEqual(engine.last_risk_daily_pnl, lifetime)


if __name__ == "__main__":
    unittest.main()
