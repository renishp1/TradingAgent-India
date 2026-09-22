from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import datetime, timedelta

from grow.backtest.costs import CostModel, SlippageModel
from grow.clock import IST
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.live_data.mock import bullish_event
from grow.live_data.models import CycleStatus, SessionHealth
from grow.paper.exits import ExitReason, choose_exit
from grow.paper.positions import PositionState
from grow.paper.valuation import unrealized_gross
from grow.types import Side
from tests.test_live_data import AS_OF, _live_config, _loop


SQUARE_OFF = datetime(2026, 9, 18, 15, 20, tzinfo=IST)


def _cfg(**kwargs):
    defaults = dict(snapshot_interval_seconds=0, session_timeout_seconds=86400, max_staleness_seconds=30)
    defaults.update(kwargs)
    return _live_config(**defaults)


def _set_quotes(payload: dict, *, bid, ask=None, ltp=None) -> dict:
    for quote in payload.get("option_quotes") or []:
        quote["bid"] = bid
        quote["ask"] = ask if ask is not None else (None if bid is None else bid + 1)
        quote["ltp"] = ltp if ltp is not None else bid
    return payload


class ExitUnitTests(unittest.TestCase):
    def test_priority_stop_then_target_then_session(self) -> None:
        self.assertEqual(choose_exit(mark_price=10, stop_loss_price=20, take_profit_price=40, session_close=True), ExitReason.STOP_LOSS)
        self.assertEqual(choose_exit(mark_price=50, stop_loss_price=20, take_profit_price=40, session_close=True), ExitReason.TAKE_PROFIT)
        self.assertEqual(choose_exit(mark_price=30, stop_loss_price=20, take_profit_price=40, session_close=True), ExitReason.SESSION_CLOSE)
        self.assertIsNone(choose_exit(mark_price=30, stop_loss_price=20, take_profit_price=40, session_close=False))


class PositionLoopTests(unittest.TestCase):
    def test_open_position_contract_and_thresholds(self) -> None:
        loop = _loop([bullish_event()], config=_cfg())
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.PAPER_FILL, report.reason)
        pos = loop.positions.open_positions()[0]
        self.assertEqual(pos.lot_size, 75)
        self.assertEqual(pos.lots, 1)
        self.assertEqual(pos.quantity, 75)
        self.assertEqual(pos.entry_price, report.fill.price)
        self.assertEqual(pos.option_type, "CE")
        self.assertEqual(pos.quantity, pos.lots * pos.lot_size)
        self.assertEqual(pos.stop_loss_price, round(pos.entry_price * 0.8, 2))
        self.assertEqual(pos.take_profit_price, round(pos.entry_price * 1.2, 2))
        self.assertIn(pos.contract_id, report.fill.symbol.ticker)
        self.assertEqual(pos.state, PositionState.OPEN)
        self.assertEqual(report.extras["position_id"], pos.position_id)

    def test_bid_mtm(self) -> None:
        loop = _loop([bullish_event(sequence=1), _set_quotes(bullish_event(sequence=2), bid=90.0, ask=91.0, ltp=90.5)], config=_cfg())
        open_report = loop.run_once("NIFTY")[0]
        self.assertEqual(open_report.status, CycleStatus.PAPER_FILL, open_report.reason)
        pos = loop.positions.open_positions()[0]
        loop.run_once("NIFTY")
        self.assertEqual(pos.state, PositionState.OPEN)
        self.assertEqual(pos.price_source, "BID")
        self.assertEqual(pos.current_price, 90.0)
        expected = unrealized_gross(mark_price=90.0, entry_price=pos.entry_price, quantity=pos.quantity)
        self.assertEqual(pos.unrealized_pnl, expected)

    def test_ltp_fallback_when_bid_missing(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), _set_quotes(bullish_event(sequence=2), bid=None, ask=91.0, ltp=88.0)],
            config=_cfg(),
        )
        loop.run_once("NIFTY")
        pos = loop.positions.open_positions()[0]
        loop.run_once("NIFTY")
        self.assertEqual(pos.price_source, "LTP")
        self.assertEqual(pos.current_price, 88.0)
        self.assertEqual(
            pos.unrealized_pnl,
            unrealized_gross(mark_price=88.0, entry_price=pos.entry_price, quantity=pos.quantity),
        )

    def test_stale_snapshot_does_not_update_mtm(self) -> None:
        loop = _loop([bullish_event(sequence=1), bullish_event(sequence=2)], config=_cfg())
        loop.run_once("NIFTY")
        pos = loop.positions.open_positions()[0]
        self.assertIsNone(pos.current_price)
        loop.clock.advance(timedelta(minutes=5))
        stale = loop.run_once("NIFTY")[0]
        self.assertTrue(stale.reason.startswith("STALE"), stale.reason)
        self.assertIsNone(pos.current_price)
        self.assertEqual(pos.unrealized_pnl, 0.0)
        self.assertEqual(pos.state, PositionState.OPEN)
        self.assertEqual(len(loop.ledger.book.fills), 1)

    def test_future_snapshot_does_not_update_mtm(self) -> None:
        future = bullish_event(sequence=2)
        later = AS_OF + timedelta(seconds=5)
        future["event_time"] = later.isoformat()
        future["received_time"] = later.isoformat()
        loop = _loop([bullish_event(sequence=1), future], config=_cfg())
        loop.run_once("NIFTY")
        pos = loop.positions.open_positions()[0]
        report = loop.run_once("NIFTY")[0]
        self.assertIn("FUTURE_SNAPSHOT", report.reason)
        self.assertIsNone(pos.current_price)
        self.assertEqual(pos.state, PositionState.OPEN)

    def test_out_of_order_snapshot_does_not_update_mtm(self) -> None:
        loop = _loop([bullish_event(sequence=2), bullish_event(sequence=1)], config=_cfg())
        first = loop.run_once("NIFTY")[0]
        self.assertEqual(first.status, CycleStatus.PAPER_FILL, first.reason)
        pos = loop.positions.open_positions()[0]
        second = loop.run_once("NIFTY")[0]
        self.assertIn("OUT_OF_ORDER", second.reason)
        self.assertIsNone(pos.current_price)
        self.assertEqual(len(loop.ledger.book.fills), 1)

    def test_missing_quote_leaves_position_open(self) -> None:
        missing = bullish_event(sequence=2)
        missing["option_quotes"] = []
        loop = _loop([bullish_event(sequence=1), missing], config=_cfg())
        loop.run_once("NIFTY")
        pos = loop.positions.open_positions()[0]
        loop.run_once("NIFTY")
        self.assertEqual(pos.state, PositionState.OPEN)
        self.assertIsNone(pos.current_price)
        self.assertTrue(any("MISSING_QUOTE" in item for item in pos.diagnostics))
        self.assertEqual(len(loop.ledger.book.fills), 1)

    def test_stop_loss_closes_once(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), _set_quotes(bullish_event(sequence=2), bid=10.0, ask=11.0, ltp=10.0)],
            config=_cfg(),
        )
        opened = loop.run_once("NIFTY")[0]
        self.assertEqual(opened.status, CycleStatus.PAPER_FILL, opened.reason)
        pos = loop.positions.all()[0]
        closed = loop.run_once("NIFTY")[0]
        self.assertEqual(closed.status, CycleStatus.PAPER_CLOSE, closed.reason)
        self.assertEqual(closed.reason, ExitReason.STOP_LOSS)
        self.assertEqual(closed.extras["quantity"], pos.quantity)
        self.assertEqual(pos.state, PositionState.CLOSED)
        self.assertEqual(pos.unrealized_pnl, 0.0)
        self.assertEqual(len([f for f in loop.ledger.book.fills if f.side is Side.SELL]), 1)
        self.assertEqual(loop.positions.closes, 1)
        self.assertEqual(loop.positions.stop_loss_exits, 1)
        self.assertNotIn(pos.contract_id, loop.ledger.book.positions)

    def test_take_profit_closes_once(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), _set_quotes(bullish_event(sequence=2), bid=200.0, ask=201.0, ltp=200.0)],
            config=_cfg(),
        )
        loop.run_once("NIFTY")
        pos = loop.positions.all()[0]
        closed = loop.run_once("NIFTY")[0]
        self.assertEqual(closed.status, CycleStatus.PAPER_CLOSE, closed.reason)
        self.assertEqual(closed.reason, ExitReason.TAKE_PROFIT)
        self.assertEqual(pos.state, PositionState.CLOSED)
        self.assertEqual(loop.positions.take_profit_exits, 1)
        self.assertEqual(pos.unrealized_pnl, 0.0)

    def test_session_close_closes_once(self) -> None:
        first = bullish_event(sequence=1, as_of=AS_OF)
        second = bullish_event(sequence=2, as_of=AS_OF)
        second["event_time"] = SQUARE_OFF.isoformat()
        second["received_time"] = SQUARE_OFF.isoformat()
        second["session_date"] = SQUARE_OFF.date().isoformat()
        for quote in second["option_quotes"]:
            quote["ts"] = SQUARE_OFF.isoformat()
        loop = _loop([first, second], config=_cfg())
        opened = loop.run_once("NIFTY")[0]
        self.assertEqual(opened.status, CycleStatus.PAPER_FILL, opened.reason)
        loop.clock.advance(SQUARE_OFF - AS_OF)
        closed = loop.run_once("NIFTY")[0]
        self.assertEqual(closed.status, CycleStatus.PAPER_CLOSE, closed.reason)
        self.assertEqual(closed.reason, ExitReason.SESSION_CLOSE)
        self.assertEqual(loop.positions.session_close_exits, 1)
        self.assertEqual(loop.positions.all()[0].state, PositionState.CLOSED)

    def test_duplicate_close_does_not_double_count(self) -> None:
        loop = _loop(
            [
                bullish_event(sequence=1),
                _set_quotes(bullish_event(sequence=2), bid=10.0, ask=11.0),
                _set_quotes(bullish_event(sequence=3), bid=8.0, ask=9.0),
            ],
            config=_cfg(),
        )
        loop.run_once("NIFTY")
        closed = loop.run_once("NIFTY")[0]
        self.assertEqual(closed.status, CycleStatus.PAPER_CLOSE)
        realized = loop.positions.summary().realized_pnl
        sells = [f for f in loop.ledger.book.fills if f.side is Side.SELL]
        self.assertEqual(len(sells), 1)
        loop.run_once("NIFTY")
        self.assertEqual(loop.positions.closes, 1)
        self.assertEqual(loop.positions.summary().realized_pnl, realized)
        self.assertEqual(len([f for f in loop.ledger.book.fills if f.side is Side.SELL]), 1)

    def test_exit_cannot_create_short(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), _set_quotes(bullish_event(sequence=2), bid=10.0, ask=11.0)],
            config=_cfg(),
        )
        loop.run_once("NIFTY")
        loop.run_once("NIFTY")
        for pos in loop.ledger.book.positions.values():
            self.assertGreaterEqual(pos.quantity, 0)
        self.assertEqual(loop.ledger.book.positions, {})

    def test_later_snapshot_cannot_change_lot_size(self) -> None:
        later = _set_quotes(bullish_event(sequence=2), bid=90.0, ask=91.0)
        for row in later["contract_master"]:
            row["lot_size"] = 1
        loop = _loop([bullish_event(sequence=1), later], config=_cfg())
        loop.run_once("NIFTY")
        pos = loop.positions.open_positions()[0]
        stored = pos.lot_size
        self.assertEqual(stored, 75)
        loop.run_once("NIFTY")
        self.assertEqual(pos.lot_size, 75)
        self.assertEqual(pos.quantity, 75)

    def test_realized_pnl_and_costs(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), _set_quotes(bullish_event(sequence=2), bid=10.0, ask=11.0)],
            config=_cfg(),
        )
        loop.run_once("NIFTY")
        pos = loop.positions.all()[0]
        closed = loop.run_once("NIFTY")[0]
        self.assertEqual(closed.status, CycleStatus.PAPER_CLOSE)
        exit_px = closed.fill.price
        slip = SlippageModel(loop.config.backtest.slippage_bps)
        expected_px, _ = slip.sell(10.0)
        self.assertEqual(exit_px, expected_px)
        gross = round((exit_px - pos.entry_price) * pos.quantity, 4)
        costs = CostModel().round_trip(entry=pos.entry_price, exit=exit_px, quantity=pos.lots, lot_size=pos.lot_size)
        self.assertEqual(closed.extras["gross_pnl"], gross)
        self.assertEqual(closed.extras["costs"], costs)
        self.assertEqual(closed.extras["net_pnl"], round(gross - costs, 4))
        self.assertEqual(pos.realized_pnl, round(gross - costs, 4))
        self.assertEqual(pos.unrealized_pnl, 0.0)

    def test_unrealized_zero_after_close(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), _set_quotes(bullish_event(sequence=2), bid=200.0, ask=201.0)],
            config=_cfg(),
        )
        loop.run_once("NIFTY")
        loop.run_once("NIFTY")
        pos = loop.positions.all()[0]
        self.assertEqual(pos.state, PositionState.CLOSED)
        self.assertEqual(pos.unrealized_pnl, 0.0)
        self.assertEqual(loop.positions.summary().unrealized_pnl, 0.0)

    def test_session_summary_reconciles(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), _set_quotes(bullish_event(sequence=2), bid=10.0, ask=11.0)],
            config=_cfg(),
        )
        loop.run_once("NIFTY")
        loop.run_once("NIFTY")
        summary = loop.positions.summary()
        pos = loop.positions.all()[0]
        self.assertEqual(summary.opens, 1)
        self.assertEqual(summary.closes, 1)
        self.assertEqual(summary.realized_pnl, pos.realized_pnl)
        self.assertEqual(summary.unrealized_pnl, 0.0)
        self.assertEqual(summary.total_pnl, pos.realized_pnl)
        self.assertEqual(summary.open_exposure, 0.0)
        self.assertEqual(summary.stop_loss_exits, 1)
        self.assertEqual(summary.net_realized_pnl, round(summary.gross_realized_pnl - summary.total_costs, 4))
        self.assertEqual(summary.net_realized_pnl, pos.realized_pnl)
        self.assertAlmostEqual(loop.ledger.book.realized_pnl, summary.gross_realized_pnl, places=4)

    def test_gross_costs_and_net_pnl_reconcile_with_ledger(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), _set_quotes(bullish_event(sequence=2), bid=10.0, ask=11.0)],
            config=_cfg(),
        )
        loop.run_once("NIFTY")
        closed = loop.run_once("NIFTY")[0]
        self.assertEqual(closed.status, CycleStatus.PAPER_CLOSE)
        summary = loop.positions.summary()
        dumped = summary.to_dict()
        self.assertEqual(dumped["net_realized_pnl"], round(dumped["gross_realized_pnl"] - dumped["total_costs"], 4))
        self.assertEqual(summary.net_realized_pnl, summary.realized_pnl)
        self.assertAlmostEqual(loop.ledger.book.realized_pnl, summary.gross_realized_pnl, places=4)
        self.assertNotEqual(summary.total_costs, 0.0)
        self.assertNotEqual(summary.gross_realized_pnl, summary.net_realized_pnl)
        self.assertEqual(closed.extras["session_summary"]["net_realized_pnl"], summary.net_realized_pnl)

    def test_timeout_with_no_open_position(self) -> None:
        loop = _loop([bullish_event()], config=_cfg(session_timeout_seconds=30))
        loop.clock.advance(timedelta(seconds=30))
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertEqual(report.reason, "SESSION_TIMEOUT")
        self.assertEqual(loop.health.state, SessionHealth.STOPPED)
        self.assertEqual(loop.positions.open_positions(), ())
        self.assertEqual(loop.ledger.book.fills, [])
        later = loop.run_once("NIFTY")[0]
        self.assertIn("STOPPED", later.reason)
        self.assertEqual(loop.ledger.book.fills, [])

    def test_timeout_with_open_position_is_unresolved(self) -> None:
        loop = _loop(
            [
                bullish_event(sequence=1),
                _set_quotes(bullish_event(sequence=2), bid=90.0, ask=91.0, ltp=90.0),
                bullish_event(sequence=3),
            ],
            config=_cfg(session_timeout_seconds=30),
        )
        opened = loop.run_once("NIFTY")[0]
        self.assertEqual(opened.status, CycleStatus.PAPER_FILL, opened.reason)
        loop.run_once("NIFTY")
        pos = loop.positions.open_positions()[0]
        mark = pos.current_price
        valued_at = pos.last_valued_at
        self.assertEqual(pos.price_source, "BID")
        fills = list(loop.ledger.book.fills)
        loop.clock.advance(timedelta(seconds=30))
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertEqual(report.reason, "SESSION_TIMEOUT_WITH_OPEN_POSITION")
        self.assertEqual(loop.health.state, SessionHealth.STOPPED)
        self.assertTrue(loop.positions.halted)
        self.assertTrue(loop.positions.unresolved_close)
        self.assertEqual(pos.state, PositionState.OPEN)
        self.assertEqual(pos.current_price, mark)
        self.assertEqual(pos.last_valued_at, valued_at)
        self.assertEqual(loop.ledger.book.fills, fills)
        self.assertEqual(len([f for f in loop.ledger.book.fills if f.side is Side.SELL]), 0)
        self.assertIn("SESSION_TIMEOUT_WITH_OPEN_POSITION", pos.diagnostics)
        later = loop.run_once("NIFTY")[0]
        self.assertEqual(later.status, CycleStatus.NO_TRADE)
        self.assertIn("STOPPED", later.reason)
        self.assertEqual(len(loop.ledger.book.fills), len(fills))
        self.assertEqual(loop.positions.open_positions()[0].state, PositionState.OPEN)
        self.assertEqual(loop.positions.closes, 0)


    def test_invalid_provider_cannot_fabricate_exit(self) -> None:
        loop = _loop(
            [
                bullish_event(sequence=1),
                {"provider": "grow.data.fixture.v1", "sequence": 2, "is_fixture": True},
            ],
            config=_cfg(),
        )
        loop.run_once("NIFTY")
        pos = loop.positions.open_positions()[0]
        report = loop.run_once("NIFTY")[0]
        self.assertIn("FIXTURE_FALLBACK_FORBIDDEN", report.reason)
        self.assertEqual(pos.state, PositionState.OPEN)
        self.assertEqual(len(loop.ledger.book.fills), 1)

    def test_stale_session_cannot_create_new_exposure(self) -> None:
        loop = _loop([bullish_event(sequence=1), bullish_event(sequence=2)], config=_cfg())
        loop.run_once("NIFTY")
        fills = len(loop.ledger.book.fills)
        loop.clock.advance(timedelta(minutes=5))
        report = loop.run_once("NIFTY")[0]
        self.assertTrue(report.reason.startswith("STALE"))
        self.assertEqual(loop.health.state, SessionHealth.STALE)
        self.assertEqual(len(loop.ledger.book.fills), fills)

    def test_end_to_end_open_mtm_stop_close_summary(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), _set_quotes(bullish_event(sequence=2), bid=12.0, ask=13.0, ltp=12.0)],
            config=_cfg(),
        )
        opened = loop.run_once("NIFTY")[0]
        self.assertEqual(opened.status, CycleStatus.PAPER_FILL, opened.reason)
        pos = loop.positions.open_positions()[0]
        self.assertEqual(pos.lot_size, 75)
        closed = loop.run_once("NIFTY")[0]
        self.assertEqual(closed.status, CycleStatus.PAPER_CLOSE)
        self.assertEqual(closed.reason, ExitReason.STOP_LOSS)
        self.assertEqual(pos.state, PositionState.CLOSED)
        self.assertEqual(pos.quantity, 75)
        self.assertEqual(pos.lot_size, 75)
        self.assertEqual(pos.unrealized_pnl, 0.0)
        summary = closed.extras["session_summary"]
        self.assertEqual(summary["opens"], 1)
        self.assertEqual(summary["closes"], 1)
        self.assertEqual(summary["stop_loss_exits"], 1)
        self.assertEqual(summary["realized_pnl"], pos.realized_pnl)
        self.assertFalse(summary["live_trading"])
        self.assertEqual(len(loop.ledger.book.fills), 2)

    def test_no_broker_and_live_trading_disabled(self) -> None:
        self.assertFalse(LIVE_TRADING_COMPILED)
        cfg = _cfg()
        self.assertFalse(cfg.live_data.live_trading)
        self.assertFalse(cfg.execution.live_trading_enabled)
        roots = [
            pathlib.Path(__file__).resolve().parents[1] / "grow" / "paper",
            pathlib.Path(__file__).resolve().parents[1] / "grow" / "live_data",
        ]
        banned = {"kiteconnect", "upstox", "dhanhq", "zerodha"}
        forbidden_names = {"LiveBroker", "place_live_order", "place_order"}
        for root in roots:
            for path in root.glob("*.py"):
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        self.assertNotEqual(node.module, "grow.execution.live")
                        for alias in node.names:
                            self.assertNotIn(alias.name, forbidden_names)
                for needle in forbidden_names:
                    self.assertNotIn(needle, source)
                for name in banned:
                    self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()
