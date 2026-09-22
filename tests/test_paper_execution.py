"""Requirement Paper — 4C decisions through paper execution, with no broker orders."""

from __future__ import annotations

import ast
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.contract import IntegratedDecisionStatus
from grow.decision.integration.integrator import DecisionIntegrator
from grow.errors import GrowSafetyError
from grow.execution import live as live_execution
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.orchestration.models import AggregateAnalysisPackage
from grow.paper.engine import PaperExecutionEngine
from grow.paper.fills import simulate_fill
from grow.paper.positions import PositionState

from tests.helpers import TEST_RISK_SECRET, make_guard


ROOT = Path(__file__).resolve().parents[1]
AS_OF = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
EXPIRY = date(2026, 9, 24)


def _quote(as_of, **overrides):
    payload = dict(
        underlying="RELIANCE",
        expiry=EXPIRY,
        strike=2500.0,
        option_type="CE",
        ltp=100.0,
        bid=99.0,
        ask=101.0,
        open_interest=10,
        volume=10,
        quote_timestamp=as_of,
        quote_age_seconds=0.0,
        provider_contract_id="RELIANCE-2500-CE",
        quality=DataQualityStatus.OK,
    )
    payload.update(overrides)
    return OptionQuoteView(**payload)


def _snapshot(as_of=AS_OF, quotes=None, quality=DataQualityStatus.OK):
    if quotes is None:
        quotes = (_quote(as_of),)
    return build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=as_of,
        spot=2500.0,
        option_contracts=tuple(quotes),
        quality=quality,
        notes=("stale",) if quality is DataQualityStatus.STALE else (),
    )


def _metrics(**overrides):
    payload = {
        "strategy": "trend",
        "direction": "BULLISH",
        "underlying": "RELIANCE",
        "limit_price": 100.0,
        "stop_loss": 80.0,
        "quantity": 1,
    }
    payload.update(overrides)
    return payload


def _result(snapshot, *, name="strategy_research", instrument="RELIANCE-2500-CE", cycle_id="cycle-paper", **kwargs):
    return AgentResult(
        agent_name=name,
        agent_version=f"{name}.v2",
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        decision_timestamp=snapshot.decision_timestamp,
        status=AgentStatus.PASS,
        observations=("spot=2500",),
        calculated_metrics=kwargs.pop("metrics", _metrics()),
        interpretation=(),
        findings=("UNANIMOUS_OPEN",),
        data_quality_concerns=(),
        assumptions=("buyer-only",),
        evidence=(f"snapshot_id={snapshot.snapshot_id}",),
        metrics_used=("limit_price", "stop_loss", "quantity"),
        candidate_action=CandidateAction.PAPER_OPEN,
        candidate_instrument=instrument,
        entry_reason="paper-test",
        invalidation_reason=None,
        risk_flags=(),
        missing_data=(),
        confidence=0.4,
        cycle_id=cycle_id,
    )


def _package(snapshot, output, *, cycle_id="cycle-paper", instrument_ok=True):
    del instrument_ok
    return AggregateAnalysisPackage(
        cycle_id=cycle_id,
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        as_of=snapshot.decision_timestamp,
        agent_outputs=(output,),
        rejected_outputs=(),
        dispatch_records=(),
        conflicts=(),
        supporting_evidence=(),
        conflicting_evidence=(),
        unavailable_agents=(),
        debate=DebateSummary(
            agreement=True,
            actions=("PAPER_OPEN",),
            agreeing_agents=(output.agent_name,),
            dissenting_agents=(),
            conflicts=(),
            evidence=(),
            insufficient_agents=(),
            error_agents=(),
        ),
        cycle_summary="paper-test",
        package_digest=f"pkg-{cycle_id}",
    )


def _config(**risk):
    config = load_config()
    config = replace(
        config,
        live_data=replace(config.live_data, session_timeout_seconds=86400),
    )
    if risk:
        config = replace(config, risk=replace(config.risk, **risk))
    return config


def _engine(config=None, clock=None):
    config = config or _config()
    clock = clock or FrozenClock(AS_OF)
    return PaperExecutionEngine(config, clock=clock, risk_secret=TEST_RISK_SECRET), clock


def _integrator(config, clock):
    return DecisionIntegrator(config, risk_guard=make_guard(config, clock=clock))


def _approved(engine_config, clock, snapshot, *, instrument="RELIANCE-2500-CE", cycle_id="cycle-paper", metrics=None):
    kwargs = {}
    if metrics is not None:
        kwargs["metrics"] = metrics
    output = _result(snapshot, instrument=instrument, cycle_id=cycle_id, **kwargs)
    package = _package(snapshot, output, cycle_id=cycle_id)
    integrator = _integrator(engine_config, clock)
    decision = integrator.integrate(snapshot=snapshot, package=package)
    return decision, package


class PaperExecutionTests(unittest.TestCase):
    def test_approved_candidate_executes_in_paper_mode(self) -> None:
        config = _config()
        engine, clock = _engine(config)
        snap = _snapshot()
        decision, package = _approved(config, clock, snap)
        self.assertTrue(decision.paper_trade_candidate)
        result = engine.run(
            _integrator(config, clock),
            snap,
            package,
        )
        self.assertTrue(result.accepted, result.reason)
        self.assertEqual(result.broker_order_calls, 0)
        self.assertEqual(engine.broker_order_calls, 0)
        self.assertFalse(decision.executed)
        self.assertFalse(decision.to_dict()["broker_order_path"])
        self.assertEqual(len(engine.positions.open_positions()), 1)
        position = engine.positions.open_positions()[0]
        order = engine.orders()[0]
        self.assertEqual(order.status, "OPEN")
        self.assertEqual(order.fill_status, "FILLED")
        self.assertEqual(order.decision_id, decision.decision_id)
        self.assertEqual(order.snapshot_id, snap.snapshot_id)
        self.assertEqual(order.snapshot_version, snap.version)
        self.assertEqual(order.instrument, "RELIANCE-2500-CE")
        self.assertEqual(order.token, "RELIANCE-2500-CE")
        self.assertEqual(order.option_type, "CE")
        self.assertEqual(order.expiry, EXPIRY)
        self.assertEqual(order.strike, 2500.0)
        self.assertEqual(order.side, "BUY")
        self.assertEqual(order.direction, "BULLISH")
        self.assertEqual(order.order_type, "LIMIT")
        self.assertEqual(order.requested_price, 100.0)
        self.assertEqual(order.execution_price, 100.0)
        self.assertEqual(order.price_source, "LTP")
        self.assertEqual(order.slippage, 0.0)
        self.assertEqual(order.position_id, position.position_id)
        self.assertTrue(order.journal_record_id)
        self.assertTrue(order.paper_mode)
        self.assertFalse(order.live_trading)
        self.assertFalse(order.broker_order_path)
        self.assertEqual(position.entry_price, 100.0)
        self.assertEqual(position.state, PositionState.OPEN)
        kinds = [row.kind for row in engine.journal.records]
        self.assertIn("CANDIDATE", kinds)
        self.assertIn("ORDER", kinds)
        self.assertIn("FILL", kinds)
        self.assertIn("POSITION", kinds)
        self.assertIn("OUTCOME", kinds)
        linked = engine.journal.records[0]
        self.assertEqual(linked.decision_id, decision.decision_id)
        self.assertEqual(linked.cycle_id, "cycle-paper")
        self.assertEqual(linked.snapshot_id, snap.snapshot_id)
        self.assertTrue(linked.agent_outputs)

        later = AS_OF + timedelta(minutes=5)
        marked = _snapshot(later, quotes=(_quote(later, ltp=104.0, bid=103.0, ask=105.0),))
        engine.on_snapshot(marked)
        self.assertEqual(position.price_source, "BID")
        self.assertEqual(position.current_price, 103.0)
        self.assertEqual(position.entry_price, 100.0)
        self.assertEqual(position.state, PositionState.OPEN)

    def test_risk_guard_rejection_prevents_execution(self) -> None:
        config = _config(max_per_trade_risk=5)
        engine, clock = _engine(config)
        snap = _snapshot()
        output = _result(snap)
        decision = _integrator(config, clock).integrate(snapshot=snap, package=_package(snap, output))
        self.assertEqual(decision.status, IntegratedDecisionStatus.BLOCKED)
        result = engine.execute(decision, snap, package=_package(snap, output))
        self.assertFalse(result.accepted)
        self.assertIn("NOT_APPROVED", result.reason)
        self.assertIn("RISK_GUARD_REJECTED", result.reason)
        self.assertEqual(engine.positions.open_positions(), ())
        self.assertEqual(engine.ledger.book.fills, [])
        self.assertEqual(engine.orders(), ())

    def test_invalid_or_stale_snapshot_prevents_entry(self) -> None:
        config = _config()
        clock = FrozenClock(AS_OF)
        stale_quote = _quote(AS_OF, quality=DataQualityStatus.STALE)
        stale = _snapshot(quotes=(stale_quote,))
        output = _result(stale)
        decision = _integrator(config, clock).integrate(snapshot=stale, package=_package(stale, output))
        self.assertEqual(decision.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertIn("DATA_STALE", decision.reason_codes)
        engine, _clock = _engine(config, clock)
        result = engine.execute(decision, stale, package=_package(stale, output))
        self.assertFalse(result.accepted)
        self.assertIn("DATA_STALE", result.reason)
        self.assertEqual(engine.positions.open_positions(), ())

        good = _snapshot()
        approved, package = _approved(config, clock, good)
        other = _snapshot(AS_OF + timedelta(minutes=1))
        mismatch = engine.execute(approved, other, package=package)
        self.assertFalse(mismatch.accepted)
        self.assertEqual(mismatch.reason, "SNAPSHOT_MISMATCH")
        self.assertEqual(len(engine.positions.open_positions()), 0)

    def test_future_price_is_not_used_for_entry(self) -> None:
        config = _config()
        clock = FrozenClock(AS_OF)
        future = _snapshot(quotes=(_quote(AS_OF, quote_timestamp=AS_OF + timedelta(minutes=1)),))
        engine, _clock = _engine(config, clock)
        decision, package = _approved(config, clock, future)
        self.assertTrue(decision.paper_trade_candidate)
        result = engine.execute(decision, future, package=package)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "FUTURE_PRICE")
        self.assertEqual(engine.ledger.book.fills, [])
        self.assertEqual(engine.positions.open_positions(), ())

    def test_duplicate_decision_does_not_create_a_second_order(self) -> None:
        config = _config()
        engine, clock = _engine(config)
        snap = _snapshot()
        decision, package = _approved(config, clock, snap)
        first = engine.execute(decision, snap, package=package)
        self.assertTrue(first.accepted, first.reason)
        before = [row.to_dict() for row in engine.journal.records]
        second = engine.execute(decision, snap, package=package)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, "DUPLICATE_DECISION")
        self.assertEqual(len(engine.positions.open_positions()), 1)
        self.assertEqual(len(engine.ledger.book.fills), 1)
        self.assertEqual(len(engine.orders()), 1)
        self.assertEqual([row.to_dict() for row in engine.journal.records[: len(before)]], before)

    def test_max_position_rule_uses_established_cap(self) -> None:
        config = _config()
        self.assertIsNone(config.risk.max_open_positions)
        self.assertEqual(config.backtest.max_open_positions, 1)
        engine, clock = _engine(config)
        snap = _snapshot(
            quotes=(
                _quote(AS_OF),
                _quote(
                    AS_OF,
                    strike=2600.0,
                    provider_contract_id="RELIANCE-2600-CE",
                ),
            )
        )
        first_decision, first_package = _approved(config, clock, snap, cycle_id="cycle-a")
        second_decision, second_package = _approved(
            config,
            clock,
            snap,
            instrument="RELIANCE-2600-CE",
            cycle_id="cycle-b",
        )
        opened = engine.execute(first_decision, snap, package=first_package)
        self.assertTrue(opened.accepted, opened.reason)
        blocked = engine.execute(second_decision, snap, package=second_package)
        self.assertFalse(blocked.accepted)
        self.assertEqual(blocked.reason, "MAX_POSITIONS")
        self.assertEqual(len(engine.positions.open_positions()), 1)
        self.assertEqual(len(engine.ledger.book.fills), 1)

    def test_daily_loss_and_per_trade_risk(self) -> None:
        loose = _config(max_per_trade_risk=1000, max_daily_loss=10, max_open_positions=2)
        tight = _config(max_per_trade_risk=5)
        clock = FrozenClock(AS_OF)
        snap = _snapshot()
        candidate, package = _approved(loose, clock, snap)
        self.assertTrue(candidate.paper_trade_candidate)
        tight_engine, _clock = _engine(tight, clock)
        per_trade = tight_engine.execute(candidate, snap, package=package)
        self.assertFalse(per_trade.accepted)
        self.assertIn("risk.per_trade", per_trade.reason)
        self.assertEqual(tight_engine.positions.open_positions(), ())

        engine, clock = _engine(loose, clock)
        decision_a, package_a = _approved(loose, clock, snap, cycle_id="cycle-loss-a")
        decision_b, package_b = _approved(
            loose,
            clock,
            _snapshot(
                quotes=(
                    _quote(AS_OF),
                    _quote(AS_OF, strike=2600.0, provider_contract_id="RELIANCE-2600-CE"),
                )
            ),
            instrument="RELIANCE-2600-CE",
            cycle_id="cycle-loss-b",
        )
        # decision_b was priced on a two-contract snapshot; execute it against that same snapshot later.
        snap_b = _snapshot(
            quotes=(
                _quote(AS_OF),
                _quote(AS_OF, strike=2600.0, provider_contract_id="RELIANCE-2600-CE"),
            )
        )
        decision_b, package_b = _approved(
            loose,
            clock,
            snap_b,
            instrument="RELIANCE-2600-CE",
            cycle_id="cycle-loss-b",
        )
        opened = engine.execute(decision_a, snap, package=package_a)
        self.assertTrue(opened.accepted, opened.reason)
        stop_at = AS_OF + timedelta(minutes=5)
        stopped = engine.on_snapshot(_snapshot(stop_at, quotes=(_quote(stop_at, ltp=70.0, bid=70.0, ask=71.0),)))
        self.assertIn("STOP_LOSS", stopped)
        closed = [row for row in engine.positions.all() if row.state is PositionState.CLOSED][0]
        self.assertEqual(closed.realized_gross, round((70.0 - 100.0) * closed.quantity, 4))
        self.assertEqual(closed.realized_pnl, round(closed.realized_gross - closed.total_costs, 4))
        summary = engine.positions.summary()
        self.assertEqual(summary.net_realized_pnl, round(summary.gross_realized_pnl - summary.total_costs, 4))
        self.assertLess(summary.net_realized_pnl, -10)
        blocked = engine.execute(decision_b, snap_b, package=package_b)
        self.assertFalse(blocked.accepted)
        self.assertIn("loss.daily", blocked.reason)
        self.assertEqual(engine.last_risk_daily_pnl, summary.net_realized_pnl)
        self.assertEqual(len([row for row in engine.positions.all() if row.state is PositionState.OPEN]), 0)

        mtm_config = _config(max_daily_loss=10, max_open_positions=2, max_per_trade_risk=1000)
        mtm_engine, mtm_clock = _engine(mtm_config)
        mtm_snap = _snapshot()
        mtm_decision, mtm_package = _approved(mtm_config, mtm_clock, mtm_snap, cycle_id="cycle-mtm")
        second, second_package = _approved(
            mtm_config,
            mtm_clock,
            _snapshot(quotes=(_quote(AS_OF), _quote(AS_OF, strike=2600.0, provider_contract_id="RELIANCE-2600-CE"))),
            instrument="RELIANCE-2600-CE",
            cycle_id="cycle-mtm-b",
        )
        snap_two = _snapshot(quotes=(_quote(AS_OF), _quote(AS_OF, strike=2600.0, provider_contract_id="RELIANCE-2600-CE")))
        second, second_package = _approved(mtm_config, mtm_clock, snap_two, instrument="RELIANCE-2600-CE", cycle_id="cycle-mtm-b")
        self.assertTrue(mtm_engine.execute(mtm_decision, mtm_snap, package=mtm_package).accepted)
        mark_at = AS_OF + timedelta(minutes=5)
        mtm_engine.on_snapshot(_snapshot(mark_at, quotes=(_quote(mark_at, ltp=85.0, bid=85.0, ask=86.0),)))
        open_position = mtm_engine.positions.open_positions()[0]
        self.assertEqual(open_position.state, PositionState.OPEN)
        self.assertLess(mtm_engine.positions.summary().total_pnl, -10)
        held = mtm_engine.execute(second, snap_two, package=second_package)
        self.assertFalse(held.accepted)
        self.assertEqual(held.reason, "DAILY_LOSS_LIMIT")
        self.assertEqual(len(mtm_engine.positions.open_positions()), 1)

    def test_fill_and_slippage_are_deterministic(self) -> None:
        config = replace(
            _config(),
            paper=replace(
                _config().paper,
                fill_model="configurable",
                entry_price_source="ASK",
                exit_price_source="BID",
                slippage_bps=10,
            ),
        )
        prices = []
        slips = []
        for _ in range(2):
            engine, clock = _engine(config)
            snap = _snapshot()
            decision, package = _approved(config, clock, snap, cycle_id="cycle-slip")
            result = engine.execute(decision, snap, package=package)
            self.assertTrue(result.accepted, result.reason)
            order = engine.orders()[0]
            prices.append(order.execution_price)
            slips.append(order.slippage)
            self.assertEqual(order.price_source, "ASK")
            self.assertEqual(order.slippage_model_version, "paper.fill.configurable.v1")
            self.assertEqual(order.fee_model_version, "costs.india.fn_o.v1")
            self.assertIn("brokerage_per_order", order.fee_assumptions)
        self.assertEqual(prices[0], prices[1])
        self.assertEqual(slips[0], slips[1])
        self.assertEqual(prices[0], round(101.0 + round(101.0 * 10 / 10_000.0, 4), 4))

        mid_config = replace(
            _config(),
            paper=replace(
                _config().paper,
                fill_model="configurable",
                entry_price_source="MIDPOINT",
                slippage_bps=0,
            ),
        )
        mid_engine, mid_clock = _engine(mid_config)
        mid_snap = _snapshot()
        mid_decision, mid_package = _approved(mid_config, mid_clock, mid_snap, cycle_id="cycle-mid")
        mid = mid_engine.execute(mid_decision, mid_snap, package=mid_package)
        self.assertTrue(mid.accepted, mid.reason)
        self.assertEqual(mid_engine.orders()[0].price_source, "MIDPOINT")
        self.assertEqual(mid_engine.orders()[0].execution_price, 100.0)
        self.assertEqual(mid_engine.orders()[0].slippage, 0.0)

    def test_entry_and_exit_point_in_time(self) -> None:
        config = _config()
        engine, clock = _engine(config)
        snap = _snapshot()
        decision, package = _approved(config, clock, snap)
        result = engine.execute(decision, snap, package=package)
        self.assertTrue(result.accepted, result.reason)
        entry_fill = next(row for row in engine.journal.records if row.kind == "FILL")
        entry_at = datetime.fromisoformat(entry_fill.payload["fill_timestamp"])
        quote_at = datetime.fromisoformat(entry_fill.payload["quote_timestamp"])
        self.assertLessEqual(quote_at, entry_at)
        self.assertEqual(entry_at, snap.decision_timestamp)
        position = engine.positions.open_positions()[0]
        close_at = datetime(2026, 9, 22, 15, 20, tzinfo=IST)
        reasons = engine.on_snapshot(_snapshot(close_at, quotes=(_quote(close_at, ltp=110.0, bid=110.0, ask=111.0),)))
        self.assertIn("SESSION_CLOSE", reasons)
        self.assertEqual(position.state, PositionState.CLOSED)
        self.assertGreater(position.closed_at, position.opened_at)
        exit_fill = [row for row in engine.journal.records if row.kind == "FILL"][-1]
        exit_at = datetime.fromisoformat(exit_fill.payload["fill_timestamp"])
        exit_quote = datetime.fromisoformat(exit_fill.payload["quote_timestamp"])
        self.assertGreater(exit_at, entry_at)
        self.assertLessEqual(exit_quote, exit_at)
        self.assertEqual(position.exit_reason, "SESSION_CLOSE")
        self.assertEqual(engine.orders()[0].status, "CLOSED")

    def test_timeout_and_expiry(self) -> None:
        timeout_config = _config()
        timeout_config = replace(timeout_config, live_data=replace(timeout_config.live_data, session_timeout_seconds=30))
        clock = FrozenClock(AS_OF)
        engine = PaperExecutionEngine(timeout_config, clock=clock, risk_secret=TEST_RISK_SECRET)
        snap = _snapshot()
        decision, package = _approved(timeout_config, clock, snap)
        self.assertTrue(engine.execute(decision, snap, package=package).accepted)
        fills_before = len(engine.ledger.book.fills)
        clock.advance(timedelta(seconds=31))
        reasons = engine.on_snapshot(_snapshot(AS_OF + timedelta(minutes=1)))
        self.assertIn("SESSION_TIMEOUT", reasons)
        position = engine.positions.open_positions()[0]
        self.assertEqual(position.state, PositionState.OPEN)
        self.assertIn("SESSION_TIMEOUT_WITH_OPEN_POSITION", position.diagnostics)
        self.assertTrue(engine.positions.halted)
        self.assertEqual(len(engine.ledger.book.fills), fills_before)
        self.assertIsNone(position.closed_at)

        expiry_config = _config()
        expiry_engine, expiry_clock = _engine(expiry_config)
        expiry_snap = _snapshot()
        expiry_decision, expiry_package = _approved(expiry_config, expiry_clock, expiry_snap, cycle_id="cycle-exp")
        self.assertTrue(expiry_engine.execute(expiry_decision, expiry_snap, package=expiry_package).accepted)
        expiry_at = datetime(2026, 9, 25, 11, 0, tzinfo=IST)
        expired = expiry_engine.on_snapshot(
            _snapshot(expiry_at, quotes=(_quote(expiry_at, ltp=90.0, bid=90.0, ask=91.0),))
        )
        self.assertIn("EXPIRED", expired)
        closed = expiry_engine.positions.all()[0]
        self.assertEqual(closed.state, PositionState.CLOSED)
        self.assertEqual(closed.exit_reason, "EXPIRED")
        self.assertEqual(closed.current_price, 90.0)
        self.assertEqual(expiry_engine.orders()[0].status, "EXPIRED")
        self.assertGreater(closed.closed_at, closed.opened_at)

    def test_restart_recovery_replays_without_rewriting_history(self) -> None:
        config = _config()
        engine, clock = _engine(config)
        snap = _snapshot()
        decision, package = _approved(config, clock, snap)
        self.assertTrue(engine.execute(decision, snap, package=package).accepted)
        first = engine.journal.records[0].to_dict()
        checkpoint = engine.export_state()
        restored = PaperExecutionEngine.restore(config, checkpoint, clock=clock, risk_secret=TEST_RISK_SECRET)
        self.assertEqual(restored.journal.to_list(), engine.journal.to_list())
        self.assertEqual(restored.journal.records[0].to_dict(), first)
        self.assertEqual(restored.positions.open_positions()[0].position_id, engine.positions.open_positions()[0].position_id)
        self.assertEqual(restored.ledger.book.cash, engine.ledger.book.cash)
        self.assertEqual(len(restored.ledger.book.fills), 1)
        again = restored.execute(decision, snap, package=package)
        self.assertFalse(again.accepted)
        self.assertEqual(again.reason, "DUPLICATE_DECISION")
        self.assertEqual(len(restored.positions.open_positions()), 1)
        self.assertEqual(len(restored.ledger.book.fills), 1)
        self.assertEqual(restored.journal.records[0].to_dict(), first)
        with self.assertRaises(GrowSafetyError):
            restored.journal.rewrite(0, {})
        with self.assertRaises(GrowSafetyError):
            restored.journal.delete(0)
        with self.assertRaises(GrowSafetyError):
            restored.journal.load(checkpoint["journal"])
        later = AS_OF + timedelta(minutes=5)
        restored.on_snapshot(_snapshot(later, quotes=(_quote(later, ltp=102.0, bid=102.0, ask=103.0),)))
        self.assertEqual(restored.positions.open_positions()[0].current_price, 102.0)
        self.assertEqual(restored.journal.records[0].to_dict(), first)

    def test_agents_cannot_change_risk_limits(self) -> None:
        config = _config()
        engine, clock = _engine(config)
        snap = _snapshot()
        decision, package = _approved(config, clock, snap)
        engine.guard.config = replace(
            engine.guard.config,
            risk=replace(engine.guard.config.risk, max_daily_loss=1_000_000_000),
        )
        with self.assertRaises(GrowSafetyError):
            engine.execute(decision, snap, package=package)
        self.assertEqual(engine.positions.open_positions(), ())
        self.assertEqual(engine.broker_order_calls, 0)

    def test_no_broker_order_api_is_called(self) -> None:
        banned_modules = {"kiteconnect", "upstox", "dhan", "grow.execution.live"}
        banned_calls = {"place_order", "place_live_order", "cancel_order"}
        root = ROOT / "grow" / "paper"
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name, banned_modules)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    self.assertFalse(module.startswith("grow.execution.live"))
                    self.assertNotIn(module, banned_modules)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, banned_calls)
        calls = {"count": 0}
        original = live_execution.place_live_order

        def _boom(*_args, **_kwargs):
            calls["count"] += 1
            raise AssertionError("broker order API was called")

        live_execution.place_live_order = _boom
        try:
            config = _config()
            engine, clock = _engine(config)
            snap = _snapshot()
            decision, package = _approved(config, clock, snap, cycle_id="cycle-broker")
            result = engine.execute(decision, snap, package=package)
            self.assertTrue(result.accepted, result.reason)
            self.assertEqual(calls["count"], 0)
            self.assertEqual(result.broker_order_calls, 0)
        finally:
            live_execution.place_live_order = original


class FillModelUnitTests(unittest.TestCase):
    def test_simulate_fill_rejects_future_quote(self) -> None:
        quote = _quote(AS_OF, quote_timestamp=AS_OF + timedelta(seconds=1))
        self.assertEqual(
            simulate_fill(
                quote,
                source="LTP",
                side="BUY",
                slippage_bps=0,
                as_of=AS_OF,
                model_version="paper.fill.deterministic.v1",
            ),
            "FUTURE_PRICE",
        )


if __name__ == "__main__":
    unittest.main()
