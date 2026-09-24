"""Campaign paper lifecycle: real CampaignOptionsAgent → paper fill / exits / timeout.

Proves the two integration fixes (lots emission; campaign session timeout disabled)
without weakening DIRECTION_CONFLICT or forcing production trades.
"""

from __future__ import annotations

import ast
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from grow.agents.campaign_options import CampaignOptionsAgent
from grow.campaign import (
    CAMPAIGN_SESSION_TIMEOUT_SECONDS,
    CampaignRunner,
    campaign_paper_config,
)
from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.contracts.agent_result import CandidateAction
from grow.decision.integration.contract import DecisionAction, IntegratedDecisionStatus
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.paper.engine import PaperExecutionEngine
from grow.paper.exits import ExitReason
from grow.paper.positions import PositionState

from tests.helpers import TEST_RISK_SECRET
from tests.test_paper_execution import _approved


ROOT = Path(__file__).resolve().parents[1]
AS_OF = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
EXPIRY = date(2026, 9, 24)
INSTRUMENT = "RELIANCE-2500-CE"


def _quote(as_of=AS_OF, **overrides) -> OptionQuoteView:
    payload = dict(
        underlying="RELIANCE",
        expiry=EXPIRY,
        strike=2500.0,
        option_type="CE",
        ltp=100.0,
        bid=99.0,
        ask=101.0,
        open_interest=5_000,
        volume=1_000,
        quote_timestamp=as_of,
        quote_age_seconds=0.0,
        provider_contract_id=INSTRUMENT,
        quality=DataQualityStatus.OK,
        lot_size=1,
        expiry_class="WEEKLY",
        implied_volatility=0.18,
        delta=0.40,
        theta=-0.05,
    )
    payload.update(overrides)
    return OptionQuoteView(**payload)


def _snapshot(as_of=AS_OF, quotes=None):
    if quotes is None:
        # CE-only chain → CampaignOptionsAgent sparse-fallback BULLISH (no conflict peers).
        quotes = (_quote(as_of),)
    return build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=as_of,
        spot=2500.0,
        option_contracts=tuple(quotes),
    )


def _campaign_runner(clock: FrozenClock) -> CampaignRunner:
    config = campaign_paper_config(load_config(environ={"GROW_EXECUTION_MODE": "paper"}))
    return CampaignRunner(
        config,
        clock=clock,
        risk_secret=TEST_RISK_SECRET,
        specialists=(CampaignOptionsAgent(config),),
        apply_campaign_defaults=False,
    )


class CampaignOptionsLotsTests(unittest.TestCase):
    def test_agent_emits_lots_and_quantity_product(self) -> None:
        snap = _snapshot()
        result = CampaignOptionsAgent(load_config()).analyze(snap, cycle_id="lots-emit")
        self.assertEqual(result.candidate_action, CandidateAction.PAPER_OPEN)
        metrics = result.calculated_metrics
        self.assertEqual(metrics["lots"], 1)
        self.assertEqual(metrics["lot_size"], 1)
        self.assertEqual(metrics["quantity"], metrics["lots"] * metrics["lot_size"])


class CampaignPaperLifecycleTests(unittest.TestCase):
    def test_a_real_campaign_options_agent_reaches_paper_fill(self) -> None:
        """A: CampaignOptionsAgent → DecisionEngine → CEO → RiskGuard → paper fill."""
        clock = FrozenClock(AS_OF)
        runner = _campaign_runner(clock)
        self.assertEqual(runner.config.live_data.session_timeout_seconds, CAMPAIGN_SESSION_TIMEOUT_SECONDS)
        self.assertEqual(CAMPAIGN_SESSION_TIMEOUT_SECONDS, 0)

        snap = _snapshot()
        agent_out = CampaignOptionsAgent(runner.config).analyze(snap, cycle_id="precheck")
        self.assertEqual(agent_out.candidate_action, CandidateAction.PAPER_OPEN)
        self.assertEqual(agent_out.calculated_metrics.get("lots"), 1)

        result = runner.run_cycle(snap, cycle_id="lifecycle-fill")
        self.assertEqual(result.decision.status, IntegratedDecisionStatus.CANDIDATE)
        self.assertIn(result.decision.action, {DecisionAction.BUY_CE, DecisionAction.BUY_PE})
        self.assertEqual(result.decision.risk_guard_result, "APPROVED")
        self.assertTrue(result.execution.accepted, msg=result.execution.reason)
        self.assertEqual(result.execution.reason, "FILLED")
        self.assertEqual(result.execution.broker_order_calls, 0)
        self.assertEqual(len(runner.paper.positions.open_positions()), 1)
        # Evidence path used by _resolve_sizing
        evidence_lots = None
        for metrics in result.decision.calculated_evidence.values():
            if isinstance(metrics, dict) and "lots" in metrics:
                evidence_lots = metrics["lots"]
                break
        self.assertEqual(evidence_lots, 1)

    def test_b_missing_lots_still_fails_closed(self) -> None:
        config = campaign_paper_config(load_config(environ={"GROW_EXECUTION_MODE": "paper"}))
        clock = FrozenClock(AS_OF)
        engine = PaperExecutionEngine(config, clock=clock, risk_secret=TEST_RISK_SECRET)
        snap = _snapshot()
        metrics = {
            "strategy": "campaign_options",
            "direction": "BULLISH",
            "underlying": "RELIANCE",
            "limit_price": 101.0,
            "stop_loss": 80.8,
            "quantity": 1,
            "lot_size": 1,
            "option_type": "CE",
            "strike": 2500.0,
            "expiry": EXPIRY.isoformat(),
            # lots intentionally omitted
        }
        from tests.test_paper_execution import _approved

        decision, package = _approved(config, clock, snap, cycle_id="nolots", metrics=metrics)
        self.assertEqual(decision.risk_guard_result, "APPROVED")
        # Confirm evidence path has no lots
        merged = {}
        for body in decision.calculated_evidence.values():
            if isinstance(body, dict):
                merged.update(body)
        self.assertNotIn("lots", merged)
        result = engine.execute(decision, snap, package=package)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "MISSING_LOTS")

    def test_c_stop_loss_closes_open_paper_position(self) -> None:
        clock = FrozenClock(AS_OF)
        runner = _campaign_runner(clock)
        opened = runner.run_cycle(_snapshot(), cycle_id="sl-open")
        self.assertTrue(opened.execution.accepted)
        pos = runner.paper.positions.open_positions()[0]
        stop = float(pos.stop_loss_price)
        later = AS_OF + timedelta(minutes=5)
        clock.advance(timedelta(minutes=5))
        mark_snap = _snapshot(
            as_of=later,
            quotes=(
                _quote(
                    later,
                    bid=max(0.05, stop - 1.0),
                    ask=max(0.10, stop - 0.5),
                    ltp=max(0.05, stop - 1.0),
                ),
            ),
        )
        reasons = runner.on_snapshot(mark_snap)
        self.assertIn(ExitReason.STOP_LOSS, reasons)
        self.assertEqual(runner.paper.positions.open_positions(), ())
        closed = runner.paper.positions.get(pos.position_id)
        self.assertIsNotNone(closed)
        assert closed is not None
        self.assertEqual(closed.state, PositionState.CLOSED)
        self.assertEqual(closed.exit_reason, ExitReason.STOP_LOSS)

    def test_d_take_profit_closes_open_paper_position(self) -> None:
        clock = FrozenClock(AS_OF)
        runner = _campaign_runner(clock)
        opened = runner.run_cycle(_snapshot(), cycle_id="tp-open")
        self.assertTrue(opened.execution.accepted)
        pos = runner.paper.positions.open_positions()[0]
        target = float(pos.take_profit_price)
        later = AS_OF + timedelta(minutes=5)
        clock.advance(timedelta(minutes=5))
        mark_snap = _snapshot(
            as_of=later,
            quotes=(
                _quote(
                    later,
                    bid=target + 1.0,
                    ask=target + 2.0,
                    ltp=target + 1.0,
                ),
            ),
        )
        reasons = runner.on_snapshot(mark_snap)
        self.assertIn(ExitReason.TAKE_PROFIT, reasons)
        closed = runner.paper.positions.get(pos.position_id)
        self.assertIsNotNone(closed)
        assert closed is not None
        self.assertEqual(closed.exit_reason, ExitReason.TAKE_PROFIT)

    def test_e_square_off_closes_open_position(self) -> None:
        clock = FrozenClock(AS_OF)
        runner = _campaign_runner(clock)
        opened = runner.run_cycle(_snapshot(), cycle_id="sq-open")
        self.assertTrue(opened.execution.accepted)
        pos = runner.paper.positions.open_positions()[0]
        square = datetime(AS_OF.year, AS_OF.month, AS_OF.day, 15, 16, tzinfo=IST)
        clock.advance(square - clock.now())
        mark_snap = _snapshot(as_of=square, quotes=(_quote(square),))
        reasons = runner.on_snapshot(mark_snap)
        self.assertIn(ExitReason.SESSION_CLOSE, reasons)
        closed = runner.paper.positions.get(pos.position_id)
        self.assertIsNotNone(closed)
        assert closed is not None
        self.assertEqual(closed.exit_reason, ExitReason.SESSION_CLOSE)

    def test_f_campaign_still_accepts_entries_after_30_seconds(self) -> None:
        """F: LivePaperLoop's 30s timeout must not halt campaign entries."""
        clock = FrozenClock(AS_OF)
        runner = _campaign_runner(clock)
        self.assertEqual(runner.config.live_data.session_timeout_seconds, 0)
        # Advance well past the old 30s LivePaperLoop default.
        clock.advance(timedelta(seconds=120))
        later = clock.now()
        snap = _snapshot(as_of=later, quotes=(_quote(later),))
        result = runner.run_cycle(snap, cycle_id="after-30s")
        self.assertNotEqual(result.execution.reason, "SESSION_TIMEOUT")
        self.assertNotEqual(result.execution.reason, "NEW_ENTRIES_BLOCKED")
        self.assertTrue(result.execution.accepted, msg=result.execution.reason)

    def test_g_no_broker_order_path(self) -> None:
        engine_src = (ROOT / "grow" / "paper" / "engine.py").read_text(encoding="utf-8")
        campaign_src = (ROOT / "grow" / "agents" / "campaign_options" / "__init__.py").read_text(
            encoding="utf-8"
        )
        for src in (engine_src, campaign_src):
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = ""
                    if isinstance(func, ast.Attribute):
                        name = func.attr
                    elif isinstance(func, ast.Name):
                        name = func.id
                    self.assertNotEqual(name, "place_order")
                    self.assertNotEqual(name, "place_live_order")
        self.assertNotIn("/orders", engine_src)

    def test_h_live_trading_compiled_remains_false(self) -> None:
        self.assertFalse(LIVE_TRADING_COMPILED)


class CampaignTimeoutConfigTests(unittest.TestCase):
    def test_default_yaml_keeps_livepaperloop_30s(self) -> None:
        raw = load_config(environ={"GROW_EXECUTION_MODE": "paper"})
        self.assertEqual(raw.live_data.session_timeout_seconds, 30)

    def test_campaign_paper_config_disables_session_timeout(self) -> None:
        cfg = campaign_paper_config(load_config(environ={"GROW_EXECUTION_MODE": "paper"}))
        self.assertEqual(cfg.live_data.session_timeout_seconds, 0)
        self.assertEqual(CAMPAIGN_SESSION_TIMEOUT_SECONDS, 0)


if __name__ == "__main__":
    unittest.main()
