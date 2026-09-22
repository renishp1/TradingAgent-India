"""Requirement Paper — 4C decisions through paper execution, with no broker orders."""

from __future__ import annotations

import ast
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from grow.clock import IST, FrozenClock
from grow.config import apply_paper_capital_profile, load_config
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
from grow.paper.fills import LTP_RESEARCH_FILL_MODEL, policy_from_config, simulate_fill
from grow.paper.positions import PositionState
from grow.paper.quotes import live_snapshot_from_agent
from grow.paper.exits import ExitReason

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
        lot_size=1,
        expiry_class="WEEKLY",
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
        "lots": 1,
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
        metrics_used=("limit_price", "stop_loss", "lots", "quantity"),
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
            self.assertEqual(order.slippage_model_version, "paper.fills.configurable.v1")
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
        # Timeout with unavailable/stale quote: stop entries, mark unresolved, do not fabricate close.
        stale = _snapshot(AS_OF + timedelta(minutes=1), quality=DataQualityStatus.STALE)
        reasons = engine.on_snapshot(stale)
        self.assertIn("SESSION_TIMEOUT", reasons)
        self.assertIn("DATA_STALE", reasons)
        position = engine.positions.open_positions()[0]
        self.assertEqual(position.state, PositionState.OPEN)
        self.assertIn("SESSION_TIMEOUT_WITH_OPEN_POSITION", position.diagnostics)
        self.assertTrue(engine.positions.halted)
        self.assertTrue(engine.positions.unresolved_close)
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


class PaperTimeoutRecoveryTests(unittest.TestCase):
    def test_timeout_with_no_open_position(self) -> None:
        config = replace(_config(), live_data=replace(_config().live_data, session_timeout_seconds=30))
        clock = FrozenClock(AS_OF)
        engine = PaperExecutionEngine(config, clock=clock, risk_secret=TEST_RISK_SECRET)
        clock.advance(timedelta(seconds=31))
        reasons = engine.on_snapshot(_snapshot(AS_OF + timedelta(minutes=1)))
        self.assertIn("SESSION_TIMEOUT", reasons)
        self.assertEqual(engine.positions.open_positions(), ())
        self.assertTrue(engine.positions.halted)
        snap = _snapshot(AS_OF + timedelta(minutes=2))
        decision, package = _approved(config, clock, snap, cycle_id="cycle-to-block")
        blocked = engine.execute(decision, snap, package=package)
        self.assertFalse(blocked.accepted)
        self.assertEqual(blocked.reason, "SESSION_TIMEOUT")

    def test_timeout_unavailable_quote_then_fresh_recovery_closes(self) -> None:
        config = replace(_config(), live_data=replace(_config().live_data, session_timeout_seconds=30))
        clock = FrozenClock(AS_OF)
        engine = PaperExecutionEngine(config, clock=clock, risk_secret=TEST_RISK_SECRET)
        snap = _snapshot()
        decision, package = _approved(config, clock, snap, cycle_id="cycle-to-rec")
        self.assertTrue(engine.execute(decision, snap, package=package).accepted)
        clock.advance(timedelta(seconds=31))
        stale = _snapshot(AS_OF + timedelta(minutes=1), quality=DataQualityStatus.STALE)
        first = engine.on_snapshot(stale)
        self.assertIn("SESSION_TIMEOUT", first)
        self.assertIn("DATA_STALE", first)
        open_pos = engine.positions.open_positions()[0]
        self.assertEqual(open_pos.state, PositionState.OPEN)
        self.assertTrue(engine.positions.unresolved_close)
        fills_before = len(engine.ledger.book.fills)

        recover_at = AS_OF + timedelta(minutes=5)
        recovered = engine.on_snapshot(
            _snapshot(recover_at, quotes=(_quote(recover_at, ltp=105.0, bid=105.0, ask=106.0),))
        )
        self.assertIn(ExitReason.SESSION_TIMEOUT, recovered)
        self.assertEqual(engine.positions.open_positions(), ())
        closed = engine.positions.all()[0]
        self.assertEqual(closed.state, PositionState.CLOSED)
        self.assertEqual(closed.exit_reason, ExitReason.SESSION_TIMEOUT)
        self.assertFalse(engine.positions.unresolved_close)
        self.assertTrue(engine.positions.halted)
        self.assertEqual(len(engine.ledger.book.fills), fills_before + 1)
        kinds = [row.kind for row in engine.journal.records]
        self.assertIn("REJECTION", kinds)
        self.assertIn("FILL", kinds)
        self.assertTrue(any(row.payload.get("reason") == "TIMEOUT_RECOVERY_CLOSED" for row in engine.journal.records))
        summary = engine.positions.summary()
        self.assertAlmostEqual(summary.net_realized_pnl, summary.gross_realized_pnl - summary.total_costs)

    def test_timeout_recovery_no_duplicate_close(self) -> None:
        config = replace(_config(), live_data=replace(_config().live_data, session_timeout_seconds=30))
        clock = FrozenClock(AS_OF)
        engine = PaperExecutionEngine(config, clock=clock, risk_secret=TEST_RISK_SECRET)
        snap = _snapshot()
        decision, package = _approved(config, clock, snap, cycle_id="cycle-to-dup")
        self.assertTrue(engine.execute(decision, snap, package=package).accepted)
        clock.advance(timedelta(seconds=31))
        engine.on_snapshot(_snapshot(AS_OF + timedelta(minutes=1), quality=DataQualityStatus.STALE))
        recover_at = AS_OF + timedelta(minutes=5)
        engine.on_snapshot(_snapshot(recover_at, quotes=(_quote(recover_at, ltp=105.0, bid=105.0, ask=106.0),)))
        fills = len(engine.ledger.book.fills)
        engine.on_snapshot(
            _snapshot(recover_at + timedelta(minutes=1), quotes=(_quote(recover_at + timedelta(minutes=1), ltp=104.0, bid=104.0, ask=105.0),))
        )
        self.assertEqual(len(engine.ledger.book.fills), fills)
        self.assertEqual(engine.positions.summary().closes, 1)
        recovery_rows = [row for row in engine.journal.records if row.payload.get("reason") == "TIMEOUT_RECOVERY_CLOSED"]
        self.assertEqual(len(recovery_rows), 1)
        sell_fills = [row for row in engine.journal.records if row.kind == "FILL" and row.payload.get("side") == "SELL"]
        self.assertEqual(len(sell_fills), 1)


class PaperLotSizeTests(unittest.TestCase):
    def test_nifty_real_lot_size_one_and_multiple_lots(self) -> None:
        config = _config(max_per_trade_risk=100_000, max_open_positions=2)
        config = replace(config, market=replace(config.market, universe=(*config.market.universe, "NIFTY")))
        engine, clock = _engine(config)
        lot_size = 75
        quote = _quote(
            AS_OF,
            underlying="NIFTY",
            strike=25000.0,
            option_type="CE",
            provider_contract_id="NIFTY-25000-CE",
            lot_size=lot_size,
            open_interest=1200,
            volume=400,
        )
        snap = build_fixture_snapshot(
            underlying="NIFTY",
            as_of=AS_OF,
            spot=25000.0,
            option_contracts=(quote,),
        )
        decision, package = _approved(
            config,
            clock,
            snap,
            instrument="NIFTY-25000-CE",
            cycle_id="cycle-nifty-1",
            metrics=_metrics(underlying="NIFTY", lots=1, quantity=75, limit_price=100.0, stop_loss=80.0),
        )
        result = engine.execute(decision, snap, package=package)
        self.assertTrue(result.accepted, result.reason)
        position = engine.positions.open_positions()[0]
        self.assertEqual(position.lot_size, 75)
        self.assertEqual(position.lots, 1)
        self.assertEqual(position.quantity, 75)
        order = engine.orders()[0]
        self.assertEqual(order.lot_size, 75)
        self.assertEqual(order.lots, 1)
        self.assertEqual(order.quantity, 75)
        fill = next(row for row in engine.journal.records if row.kind == "FILL")
        self.assertEqual(fill.payload["lot_size"], 75)
        self.assertEqual(fill.payload["lots"], 1)
        self.assertEqual(fill.payload["quantity"], 75)
        self.assertEqual(fill.payload["notional"], round(75 * fill.payload["execution_price"], 2))
        self.assertEqual(engine.ledger.book.positions[order.symbol].quantity, 75)

        engine2, clock2 = _engine(config)
        decision2, package2 = _approved(
            config,
            clock2,
            snap,
            instrument="NIFTY-25000-CE",
            cycle_id="cycle-nifty-2",
            metrics=_metrics(underlying="NIFTY", lots=2, quantity=150, limit_price=100.0, stop_loss=80.0),
        )
        result2 = engine2.execute(decision2, snap, package=package2)
        self.assertTrue(result2.accepted, result2.reason)
        pos2 = engine2.positions.open_positions()[0]
        self.assertEqual(pos2.lot_size, 75)
        self.assertEqual(pos2.lots, 2)
        self.assertEqual(pos2.quantity, 150)
        close_at = datetime(2026, 9, 22, 15, 20, tzinfo=IST)
        engine2.on_snapshot(
            build_fixture_snapshot(
                underlying="NIFTY",
                as_of=close_at,
                spot=25000.0,
                option_contracts=(_quote(close_at, underlying="NIFTY", strike=25000.0, provider_contract_id="NIFTY-25000-CE", lot_size=75, ltp=110.0, bid=110.0, ask=111.0),),
            )
        )
        closed = engine2.positions.all()[0]
        self.assertEqual(closed.state, PositionState.CLOSED)
        self.assertAlmostEqual(closed.realized_gross, round((110.0 - closed.entry_price) * 150, 4))

    def test_missing_lot_size_rejects(self) -> None:
        config = _config()
        engine, clock = _engine(config)
        snap = _snapshot(quotes=(_quote(AS_OF, lot_size=None),))
        decision, package = _approved(config, clock, snap, cycle_id="cycle-nolot", metrics=_metrics(quantity=1))
        result = engine.execute(decision, snap, package=package)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "MISSING_LOT_SIZE")
        self.assertEqual(engine.positions.open_positions(), ())

    def test_no_hardcoded_lot_size_one_in_paper_execution(self) -> None:
        root = ROOT / "grow" / "paper"
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("lot_size=1", text.replace(" ", ""), msg=str(path))


class PaperProvenanceTests(unittest.TestCase):
    def test_fixture_and_live_provenance_preserved(self) -> None:
        fixture = _snapshot()
        self.assertTrue(fixture.is_fixture)
        live_like = type(fixture)(
            snapshot_id=fixture.snapshot_id,
            version=fixture.version,
            schema=fixture.schema,
            provider="kite_market",
            exchange=fixture.exchange,
            session_timestamp=fixture.session_timestamp,
            decision_timestamp=fixture.decision_timestamp,
            session_date=fixture.session_date,
            underlyings=fixture.underlyings,
            option_contracts=(
                _quote(
                    AS_OF,
                    open_interest=555,
                    volume=321,
                    previous_open_interest=500,
                    implied_volatility=0.18,
                    delta=0.45,
                    gamma=0.01,
                    theta=-0.02,
                    vega=0.12,
                    expiry_class="WEEKLY",
                    lot_size=1,
                ),
            ),
            data_quality=fixture.data_quality,
            quality_notes=fixture.quality_notes,
            source_snapshot_ids=fixture.source_snapshot_ids,
            diagnostics={"fixture": False, "adapter_version": "kite.v9"},
            paper_mode=True,
            live_trading=False,
            is_fixture=False,
        )
        mapped = live_snapshot_from_agent(live_like, sequence=7)
        chain = mapped.chains["RELIANCE"]
        self.assertFalse(chain.is_fixture)
        self.assertEqual(mapped.provider_id, "kite_market")
        self.assertEqual(mapped.adapter_version, "kite.v9")
        self.assertEqual(mapped.snapshot_id, live_like.snapshot_id)
        self.assertTrue(chain.provider_metadata["paper_execution"])
        self.assertFalse(chain.provider_metadata["live_trading"])
        self.assertFalse(chain.provider_metadata["broker_order_path"])
        contract = chain.contracts[0]
        self.assertEqual(contract.open_interest, 555)
        self.assertEqual(contract.volume, 321)
        self.assertEqual(contract.previous_open_interest, 500)
        self.assertEqual(contract.implied_volatility, 0.18)
        self.assertEqual(contract.delta, 0.45)
        self.assertEqual(contract.gamma, 0.01)
        self.assertEqual(contract.theta, -0.02)
        self.assertEqual(contract.vega, 0.12)
        self.assertEqual(contract.provider_contract_id, "RELIANCE-2500-CE")
        self.assertEqual(contract.timestamp, AS_OF)

        fixture_mapped = live_snapshot_from_agent(fixture, sequence=1)
        self.assertTrue(fixture_mapped.chains["RELIANCE"].is_fixture)


class PaperCapitalAndPriceModeTests(unittest.TestCase):
    def test_india_index_options_paper_10k_profile(self) -> None:
        config = apply_paper_capital_profile(load_config(), "INDIA_INDEX_OPTIONS_PAPER_10K")
        self.assertEqual(config.paper.starting_cash, 10_000)
        self.assertEqual(config.risk.max_daily_loss, 2_000)
        self.assertEqual(config.risk.max_per_trade_risk, 1_000)
        self.assertEqual(config.risk.max_open_positions, 2)
        self.assertEqual(config.paper.capital_profile, "INDIA_INDEX_OPTIONS_PAPER_10K")
        # Global defaults remain unchanged when profile is not applied.
        baseline = load_config()
        self.assertEqual(baseline.paper.starting_cash, 1_000_000)
        engine, clock = _engine(config)
        self.assertEqual(engine.positions.starting_cash, 10_000)
        snap = _snapshot()
        # Notional 100 exceeds max_per_trade_risk? 1 * 100 = 100 < 1000 — allowed.
        decision, package = _approved(config, clock, snap, cycle_id="cycle-10k")
        self.assertTrue(engine.execute(decision, snap, package=package).accepted)
        # Second open blocked by max_open_positions=2 only after two opens; craft a second instrument.
        snap2 = _snapshot(quotes=(_quote(AS_OF, strike=2600.0, provider_contract_id="RELIANCE-2600-CE"),))
        # Need fresh engine state with two positions capacity — open second then third blocked.
        decision2, package2 = _approved(
            config,
            clock,
            snap2,
            instrument="RELIANCE-2600-CE",
            cycle_id="cycle-10k-b",
            metrics=_metrics(limit_price=100.0, stop_loss=80.0, quantity=1),
        )
        self.assertTrue(engine.execute(decision2, snap2, package=package2).accepted)
        snap3 = _snapshot(quotes=(_quote(AS_OF, strike=2700.0, provider_contract_id="RELIANCE-2700-CE"),))
        decision3, package3 = _approved(
            config,
            clock,
            snap3,
            instrument="RELIANCE-2700-CE",
            cycle_id="cycle-10k-c",
            metrics=_metrics(limit_price=100.0, stop_loss=80.0, quantity=1),
        )
        blocked = engine.execute(decision3, snap3, package=package3)
        self.assertFalse(blocked.accepted)
        self.assertEqual(blocked.reason, "MAX_POSITIONS")

    def test_execution_price_modes(self) -> None:
        conservative = replace(
            _config(),
            paper=replace(_config().paper, price_mode="conservative", slippage_bps=10, fill_model="configurable"),
        )
        policy = policy_from_config(conservative)
        self.assertEqual(policy.entry_source, "ASK")
        self.assertEqual(policy.exit_source, "BID")
        self.assertEqual(policy.version, "paper.fills.conservative.v1")
        engine, clock = _engine(conservative)
        snap = _snapshot()
        decision, package = _approved(conservative, clock, snap, cycle_id="cycle-cons")
        result = engine.execute(decision, snap, package=package)
        self.assertTrue(result.accepted, result.reason)
        order = engine.orders()[0]
        self.assertEqual(order.price_source, "ASK")
        fill = next(row for row in engine.journal.records if row.kind == "FILL")
        self.assertEqual(fill.payload["price_source"], "ASK")
        self.assertIn("reference_price", fill.payload)
        self.assertEqual(fill.payload["fill_model_version"], "paper.fills.conservative.v1")
        self.assertEqual(fill.payload["slippage_bps"], 10)

        mid = replace(_config(), paper=replace(_config().paper, price_mode="midpoint", slippage_bps=0, fill_model="configurable"))
        mid_policy = policy_from_config(mid)
        self.assertEqual(mid_policy.entry_source, "MIDPOINT")
        self.assertEqual(mid_policy.exit_source, "MIDPOINT")

        ltp = replace(_config(), paper=replace(_config().paper, price_mode="ltp", slippage_bps=0, fill_model="configurable"))
        ltp_policy = policy_from_config(ltp)
        self.assertTrue(ltp_policy.research_only)
        self.assertEqual(ltp_policy.version, LTP_RESEARCH_FILL_MODEL)


if __name__ == "__main__":
    unittest.main()
