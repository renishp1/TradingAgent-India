"""Phase 10 — complete paper-session runner + session summary artifact."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from grow.campaign import (
    SESSION_RUNNER_VERSION,
    SESSION_SUMMARY_SCHEMA,
    CampaignRunner,
    PaperSessionRunner,
    PaperSessionSummary,
    campaign_paper_config,
)
from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.contract import DecisionAction, DecisionBookState, IntegratedDecisionStatus
from grow.errors import GrowSafetyError
from grow.live_data.loop import LivePaperLoop
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.paper.engine import PaperExecutionEngine
from grow.paper.exits import ExitReason
from grow.paper.positions import PositionState

from tests.helpers import TEST_RISK_SECRET, make_guard


ROOT = Path(__file__).resolve().parents[1]
AS_OF = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
EXPIRY = date(2026, 9, 24)


def _quote(as_of=AS_OF, **overrides):
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


def _snapshot(as_of=AS_OF, *, quotes=None, quality=DataQualityStatus.OK):
    if quotes is None:
        quotes = (_quote(as_of),)
    return build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=as_of,
        spot=2500.0,
        option_contracts=tuple(quotes),
        quality=quality,
        notes=("phase10",) if quality is not DataQualityStatus.OK else (),
    )


class _OpenSpecialist:
    agent_name = "strategy_research"
    agent_version = "strategy_research.v2"

    def __init__(self, *, instrument="RELIANCE-2500-CE", direction="BULLISH", option_type="CE") -> None:
        self.instrument = instrument
        self.direction = direction
        self.option_type = option_type

    def analyze(self, snapshot, *, cycle_id: str = ""):
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=("phase10",),
            calculated_metrics={
                "strategy": "trend",
                "direction": self.direction,
                "underlying": "RELIANCE",
                "limit_price": 100.0,
                "stop_loss": 80.0,
                "target": 140.0 if self.option_type == "CE" else 60.0,
                "lots": 1,
                "quantity": 1,
                "option_type": self.option_type,
                "strike": 2500.0,
                "expiry": EXPIRY.isoformat(),
                "lot_size": 1,
            },
            interpretation=(),
            findings=("UNANIMOUS_OPEN",),
            data_quality_concerns=(),
            assumptions=("buyer-only",),
            evidence=(f"snapshot_id={snapshot.snapshot_id}",),
            metrics_used=("limit_price", "stop_loss"),
            candidate_action=CandidateAction.PAPER_OPEN,
            candidate_instrument=self.instrument,
            entry_reason="phase10",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
            confidence=0.5,
            cycle_id=cycle_id,
        )

    def analyze_input(self, agent_input):
        return self.analyze(agent_input.snapshot, cycle_id=agent_input.cycle_id)


def _session(*, specialists=None, clock=None, timeout_seconds=86400) -> tuple[PaperSessionRunner, FrozenClock]:
    config = campaign_paper_config(load_config())
    config = replace(config, live_data=replace(config.live_data, session_timeout_seconds=timeout_seconds))
    frozen = clock if clock is not None else FrozenClock(AS_OF)
    guard = make_guard(config, clock=frozen)
    runner = PaperSessionRunner(
        config,
        clock=frozen,
        risk_guard=guard,
        risk_secret=TEST_RISK_SECRET,
        specialists=specialists if specialists is not None else (_OpenSpecialist(),),
        apply_campaign_defaults=False,
    )
    return runner, frozen


REQUIRED_SUMMARY_KEYS = {
    "session_id",
    "market_data_health",
    "decisions",
    "rejected_decisions",
    "trades",
    "entries",
    "exits",
    "mtm",
    "gross_pnl",
    "charges",
    "net_pnl",
    "max_drawdown",
    "win_loss",
    "no_trade_reasons",
    "risk_guard_rejections",
    "provider_health",
    "agent_latency",
    "snapshot_ids",
    "decision_ids",
}


class Phase10SessionRunnerTests(unittest.TestCase):
    def test_full_session_produces_required_summary_fields(self) -> None:
        runner, _clock = _session()
        session_id = runner.start()
        self.assertEqual(runner.status, "RUNNING")
        self.assertTrue(session_id)

        opened = runner.process_snapshot(_snapshot(), cycle_id="cycle-p10-open", monitor=False)
        self.assertFalse(opened["skipped_decision"])
        self.assertTrue(opened["cycle"]["execution"]["accepted"])
        self.assertEqual(opened["broker_order_calls"], 0)

        later = AS_OF + timedelta(minutes=5)
        runner.monitor_only(
            _snapshot(later, quotes=(_quote(later, ltp=110.0, bid=109.0, ask=111.0),))
        )
        summary = runner.end()
        self.assertEqual(runner.status, "ENDED")
        self.assertIsInstance(summary, PaperSessionSummary)
        payload = summary.to_dict()
        self.assertEqual(payload["schema"], SESSION_SUMMARY_SCHEMA)
        self.assertEqual(payload["session_id"], session_id)
        for key in REQUIRED_SUMMARY_KEYS:
            self.assertIn(key, payload)
        self.assertTrue(payload["paper_mode"])
        self.assertFalse(payload["live_trading"])
        self.assertFalse(payload["broker_order_path"])
        self.assertEqual(payload["broker_order_calls"], 0)
        self.assertEqual(payload["market_data_health"], DataQualityStatus.OK.value)
        self.assertGreaterEqual(len(payload["decisions"]), 1)
        self.assertGreaterEqual(len(payload["entries"]), 1)
        self.assertEqual(runner.runner_version, SESSION_RUNNER_VERSION)

    def test_stale_market_skips_new_decision_but_monitors(self) -> None:
        runner, clock = _session()
        runner.start()
        runner.process_snapshot(_snapshot(), cycle_id="cycle-p10-pre", monitor=False)
        clock.advance(timedelta(minutes=1))
        stale_at = AS_OF + timedelta(minutes=1)
        result = runner.process_snapshot(
            _snapshot(stale_at, quality=DataQualityStatus.STALE),
            cycle_id="cycle-p10-stale",
            decide=True,
            monitor=True,
        )
        self.assertTrue(result["skipped_decision"])
        self.assertIsNone(result["cycle"])
        self.assertIn("DATA_STALE", result["monitor_reasons"])
        self.assertEqual(len(runner.cycles), 1)

    def test_risk_guard_block_recorded_in_summary(self) -> None:
        runner, _clock = _session()
        runner.start()
        result = runner.process_snapshot(
            _snapshot(),
            cycle_id="cycle-p10-block",
            monitor=False,
            book=DecisionBookState(
                cash=10_000.0,
                gross_notional=0.0,
                daily_pnl=-20_000.0,
                symbol_notional=0.0,
                open_positions=0,
            ),
        )
        cycle = result["cycle"]
        self.assertEqual(cycle["decision_status"], IntegratedDecisionStatus.BLOCKED.value)
        self.assertEqual(cycle["decision_action"], DecisionAction.NO_TRADE.value)
        self.assertFalse(cycle["execution"]["accepted"])
        summary = runner.end().to_dict()
        self.assertGreaterEqual(len(summary["risk_guard_rejections"]), 1)
        self.assertGreaterEqual(len(summary["no_trade_reasons"]), 1)
        self.assertGreaterEqual(len(summary["rejected_decisions"]), 1)

    def test_take_profit_exit_appears_in_summary(self) -> None:
        runner, _clock = _session()
        runner.start()
        runner.process_snapshot(_snapshot(), cycle_id="cycle-p10-tp-open", monitor=False)
        self.assertEqual(len(runner.paper.positions.open_positions()), 1)
        hit = AS_OF + timedelta(minutes=10)
        runner.monitor_only(
            _snapshot(hit, quotes=(_quote(hit, ltp=150.0, bid=149.0, ask=151.0),))
        )
        closed = runner.paper.positions.all()[0]
        self.assertEqual(closed.state, PositionState.CLOSED)
        self.assertEqual(closed.exit_reason, ExitReason.TAKE_PROFIT)
        summary = runner.end().to_dict()
        self.assertGreaterEqual(len(summary["exits"]), 1)
        self.assertEqual(summary["exits"][0]["exit_reason"], ExitReason.TAKE_PROFIT)
        self.assertEqual(summary["win_loss"]["closed_trades"], 1)
        self.assertEqual(summary["broker_order_calls"], 0)

    def test_write_summary_artifact(self) -> None:
        runner, _clock = _session(specialists=())
        runner.start()
        runner.process_snapshot(_snapshot(quotes=()), cycle_id="cycle-p10-empty", monitor=False)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session-summary.json"
            written = runner.write_summary(path)
            self.assertEqual(written, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], SESSION_SUMMARY_SCHEMA)
            for key in REQUIRED_SUMMARY_KEYS:
                self.assertIn(key, payload)
            runner.end()

    def test_cannot_process_before_start_or_after_end(self) -> None:
        runner, _clock = _session()
        with self.assertRaises(GrowSafetyError):
            runner.process_snapshot(_snapshot(), monitor=False)
        runner.start()
        runner.end()
        with self.assertRaises(GrowSafetyError):
            runner.process_snapshot(_snapshot(), monitor=False)

    def test_lifecycle_created_running_ended_is_terminal(self) -> None:
        """CREATED → RUNNING → ENDED; start() after ENDED must fail closed."""
        runner, _clock = _session()
        self.assertEqual(runner.status, "CREATED")
        session_id = runner.start()
        self.assertEqual(runner.status, "RUNNING")
        self.assertTrue(session_id)

        runner.process_snapshot(_snapshot(), cycle_id="cycle-p10-life-open", monitor=False)
        self.assertEqual(len(runner.paper.positions.open_positions()), 1)
        open_ids = {row.position_id for row in runner.paper.positions.open_positions()}
        cash_after_fill = runner.paper.ledger.book.cash
        fills_after_fill = len(runner.paper.ledger.book.fills)

        summary = runner.end()
        self.assertEqual(runner.status, "ENDED")
        self.assertEqual(summary.status, "ENDED")

        with self.assertRaises(GrowSafetyError) as start_ctx:
            runner.start()
        self.assertIn("ENDED", str(start_ctx.exception))

        with self.assertRaises(GrowSafetyError):
            runner.process_snapshot(_snapshot(), cycle_id="cycle-p10-life-again", monitor=False)

        # Inherited paper book must remain the ended session's book — not a fresh session.
        self.assertEqual({row.position_id for row in runner.paper.positions.open_positions()}, open_ids)
        self.assertEqual(runner.paper.ledger.book.cash, cash_after_fill)
        self.assertEqual(len(runner.paper.ledger.book.fills), fills_after_fill)
        self.assertEqual(len(runner.cycles), 1)
        self.assertEqual(runner.paper.broker_order_calls, 0)

    def test_live_trading_snapshot_rejected(self) -> None:
        runner, _clock = _session()
        runner.start()
        snap = _snapshot()
        object.__setattr__(snap, "live_trading", True)
        with self.assertRaises(ValueError):
            runner.process_snapshot(snap, monitor=False)

    def test_uses_existing_campaign_and_paper_engine(self) -> None:
        runner, _clock = _session()
        self.assertIsInstance(runner.campaign, CampaignRunner)
        self.assertIsInstance(runner.paper, PaperExecutionEngine)
        self.assertTrue(callable(LivePaperLoop))

    def test_no_broker_in_session_package(self) -> None:
        banned = {"place_live_order", "place_order", "LiveBroker", "kiteconnect"}
        for path in (ROOT / "grow" / "campaign").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, banned)
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, banned)
            self.assertNotIn("grow.execution.live", source)
        session_src = (ROOT / "grow" / "campaign" / "session.py").read_text(encoding="utf-8")
        self.assertIn("CampaignRunner", session_src)
        self.assertIn("PaperExecutionEngine", session_src)
        self.assertIn("SESSION_RUNNER_VERSION", session_src)
        self.assertNotIn("class SessionFillEngine", session_src)


if __name__ == "__main__":
    unittest.main()
