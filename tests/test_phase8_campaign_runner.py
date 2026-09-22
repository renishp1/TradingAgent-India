"""Phase 8 — single campaign runner 4B → 4C → Risk → PaperExecutionEngine."""

from __future__ import annotations

import ast
import unittest
from datetime import date, datetime
from pathlib import Path

from grow.campaign import (
    CAMPAIGN_PRICE_MODE,
    CAMPAIGN_RUNNER_VERSION,
    CampaignRunner,
    campaign_paper_config,
)
from grow.clock import IST, FrozenClock, SystemClock
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.contract import DecisionAction, DecisionBookState, IntegratedDecisionStatus
from grow.live_data.loop import LivePaperLoop
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.orchestration.models import AggregateAnalysisPackage
from grow.paper.engine import PaperExecutionEngine
from grow.paper.fills import CONSERVATIVE_FILL_MODEL

from tests.helpers import TEST_RISK_SECRET, make_guard


ROOT = Path(__file__).resolve().parents[1]
AS_OF = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
EXPIRY = date(2026, 9, 24)


def _quote(**overrides):
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
        quote_timestamp=AS_OF,
        quote_age_seconds=0.0,
        provider_contract_id="RELIANCE-2500-CE",
        quality=DataQualityStatus.OK,
        lot_size=1,
        expiry_class="WEEKLY",
    )
    payload.update(overrides)
    return OptionQuoteView(**payload)


def _snapshot(quotes=None):
    if quotes is None:
        quotes = (_quote(),)
    return build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=AS_OF,
        spot=2500.0,
        option_contracts=tuple(quotes),
    )


class _OpenSpecialist:
    """Deterministic specialist that proposes a buyer-only PAPER_OPEN candidate."""

    agent_name = "strategy_research"
    agent_version = "strategy_research.v2"

    def __init__(
        self,
        *,
        instrument="RELIANCE-2500-CE",
        direction="BULLISH",
        option_type="CE",
        strike: float = 2500.0,
    ) -> None:
        self.instrument = instrument
        self.direction = direction
        self.option_type = option_type
        self.strike = strike

    def analyze(self, snapshot, *, cycle_id: str = ""):
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=("campaign specialist",),
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
                "strike": self.strike,
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
            entry_reason="phase8",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
            confidence=0.5,
            cycle_id=cycle_id,
        )

    def analyze_input(self, agent_input):
        return self.analyze(agent_input.snapshot, cycle_id=agent_input.cycle_id)


def _runner(*, specialists=None, clock=None) -> CampaignRunner:
    config = campaign_paper_config(load_config())
    frozen = clock if clock is not None else FrozenClock(AS_OF)
    guard = make_guard(config, clock=frozen)
    return CampaignRunner(
        config,
        clock=frozen,
        risk_guard=guard,
        risk_secret=TEST_RISK_SECRET,
        specialists=specialists if specialists is not None else (_OpenSpecialist(),),
        apply_campaign_defaults=False,
    )


def _package(snapshot, result, *, cycle_id="cycle-p8", digest="pkg-phase8"):
    return AggregateAnalysisPackage(
        cycle_id=cycle_id,
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        as_of=snapshot.decision_timestamp,
        agent_outputs=(result,),
        rejected_outputs=(),
        dispatch_records=(),
        conflicts=(),
        supporting_evidence=("pkg",),
        conflicting_evidence=(),
        unavailable_agents=(),
        debate=DebateSummary(
            agreement=True,
            actions=("PAPER_OPEN",),
            agreeing_agents=(result.agent_name,),
            dissenting_agents=(),
            conflicts=(),
            evidence=(),
            insufficient_agents=(),
            error_agents=(),
        ),
        cycle_summary="phase8",
        package_digest=digest,
    )


class CampaignConfigTests(unittest.TestCase):
    def test_campaign_defaults_are_conservative_and_10k(self) -> None:
        cfg = campaign_paper_config(load_config())
        self.assertEqual(cfg.paper.price_mode, CAMPAIGN_PRICE_MODE)
        self.assertEqual(cfg.paper.starting_cash, 10_000)
        self.assertEqual(cfg.risk.max_daily_loss, 2_000)
        self.assertEqual(cfg.risk.max_per_trade_risk, 1_000)
        self.assertEqual(cfg.risk.max_open_positions, 2)
        baseline = load_config()
        self.assertNotEqual(baseline.paper.starting_cash, 10_000)
        self.assertIsNone(baseline.paper.price_mode)


class CampaignClockTests(unittest.TestCase):
    def test_default_campaign_runner_uses_system_clock(self) -> None:
        runner = CampaignRunner(
            risk_secret=TEST_RISK_SECRET,
            specialists=(),
        )
        self.assertIsInstance(runner.clock, SystemClock)
        self.assertIsInstance(runner.guard.clock, SystemClock)
        self.assertIsInstance(runner.paper.clock, SystemClock)
        self.assertIs(runner.guard.clock, runner.clock)
        self.assertIs(runner.paper.clock, runner.clock)

    def test_injected_frozen_clock_is_preserved(self) -> None:
        clock = FrozenClock(AS_OF)
        runner = _runner(clock=clock)
        self.assertIs(runner.clock, clock)
        self.assertIs(runner.guard.clock, clock)
        self.assertIs(runner.paper.clock, clock)
        self.assertIsInstance(runner.clock, FrozenClock)


class CampaignRunnerTests(unittest.TestCase):
    def test_full_cycle_buys_ce_with_conservative_ask(self) -> None:
        """13. Existing BUY_CE conservative ASK fill remains passing."""
        snap = _snapshot()
        runner = _runner()
        self.assertEqual(runner.fill_policy_version, CONSERVATIVE_FILL_MODEL)
        result = runner.run_cycle(snap, cycle_id="cycle-p8-full")
        self.assertEqual(result.runner_version, CAMPAIGN_RUNNER_VERSION)
        self.assertEqual(result.decision.status, IntegratedDecisionStatus.CANDIDATE)
        self.assertEqual(result.decision.action, DecisionAction.BUY_CE)
        self.assertIsNotNone(result.decision.campaign_signal)
        self.assertTrue(result.execution.accepted, result.execution.reason)
        self.assertEqual(result.execution.price_source, "ASK")
        self.assertGreaterEqual(result.execution.execution_price, 101.0)
        self.assertEqual(result.execution.broker_order_calls, 0)
        payload = result.to_dict()
        self.assertTrue(payload["paper_mode"])
        self.assertFalse(payload["live_trading"])
        self.assertFalse(payload["broker_order_path"])

    def test_run_from_package_path(self) -> None:
        snap = _snapshot()
        specialist = _OpenSpecialist()
        result_row = specialist.analyze(snap, cycle_id="cycle-p8")
        outcome = _runner(specialists=()).run_from_package(snap, _package(snap, result_row))
        self.assertTrue(outcome.execution.accepted, outcome.execution.reason)
        self.assertEqual(outcome.decision.action, DecisionAction.BUY_CE)

    def test_buy_pe_cycle(self) -> None:
        """14. Existing BUY_PE conservative ASK fill remains passing."""
        snap = _snapshot(quotes=(_quote(option_type="PE", provider_contract_id="RELIANCE-2500-PE"),))
        runner = _runner(
            specialists=(_OpenSpecialist(instrument="RELIANCE-2500-PE", direction="BEARISH", option_type="PE"),)
        )
        result = runner.run_cycle(snap, cycle_id="cycle-p8-pe")
        self.assertEqual(result.decision.action, DecisionAction.BUY_PE)
        self.assertTrue(result.execution.accepted, result.execution.reason)
        self.assertEqual(result.execution.price_source, "ASK")
        self.assertEqual(result.execution.broker_order_calls, 0)

    def test_missing_chain_is_no_trade(self) -> None:
        snap = _snapshot(quotes=())
        result = _runner().run_cycle(snap, cycle_id="cycle-p8-empty")
        self.assertEqual(result.decision.action, DecisionAction.NO_TRADE)
        self.assertFalse(result.execution.accepted)

    def test_risk_guard_block_no_paper_fill(self) -> None:
        """7. Risk Guard BLOCK → decision NO_TRADE → PaperExecutionEngine does not fill."""
        snap = _snapshot()
        runner = _runner()
        result = runner.run_cycle(
            snap,
            cycle_id="cycle-p8-block",
            book=DecisionBookState(
                cash=10_000.0,
                gross_notional=0.0,
                daily_pnl=-20_000.0,
                symbol_notional=0.0,
                open_positions=0,
            ),
        )
        self.assertEqual(result.decision.status, IntegratedDecisionStatus.BLOCKED)
        self.assertEqual(result.decision.action, DecisionAction.NO_TRADE)
        self.assertFalse(result.execution.accepted)
        self.assertEqual(result.execution.broker_order_calls, 0)
        self.assertEqual(len(runner.paper.positions.open_positions()), 0)

    def test_max_daily_loss_2000_blocks_new_entry(self) -> None:
        """8. Max daily loss ₹2,000 prevents a new entry."""
        snap = _snapshot()
        runner = _runner()
        self.assertEqual(runner.config.risk.max_daily_loss, 2_000)
        result = runner.run_cycle(
            snap,
            cycle_id="cycle-p8-daily-loss",
            book=DecisionBookState(
                cash=10_000.0,
                gross_notional=0.0,
                daily_pnl=-2_001.0,
                symbol_notional=0.0,
                open_positions=0,
            ),
        )
        self.assertEqual(result.decision.status, IntegratedDecisionStatus.BLOCKED)
        self.assertEqual(result.decision.action, DecisionAction.NO_TRADE)
        self.assertFalse(result.execution.accepted)
        self.assertEqual(len(runner.paper.positions.open_positions()), 0)

    def test_max_open_positions_two_blocks_third_entry(self) -> None:
        """9. Max open positions 2 prevents a third entry."""
        runner = _runner()
        self.assertEqual(runner.config.risk.max_open_positions, 2)
        specs = (
            (2500.0, "cycle-p8-pos-a", "pkg-a"),
            (2600.0, "cycle-p8-pos-b", "pkg-b"),
            (2700.0, "cycle-p8-pos-c", "pkg-c"),
        )
        for strike, cycle_id, digest in specs[:2]:
            snap = _snapshot(
                quotes=(_quote(strike=strike, provider_contract_id=f"RELIANCE-{int(strike)}-CE"),)
            )
            specialist = _OpenSpecialist(
                instrument=f"RELIANCE-{int(strike)}-CE",
                strike=strike,
            )
            row = specialist.analyze(snap, cycle_id=cycle_id)
            outcome = runner.run_from_package(
                snap,
                _package(snap, row, cycle_id=cycle_id, digest=digest),
            )
            self.assertTrue(outcome.execution.accepted, outcome.execution.reason)
        self.assertEqual(len(runner.paper.positions.open_positions()), 2)
        snap3 = _snapshot(quotes=(_quote(strike=2700.0, provider_contract_id="RELIANCE-2700-CE"),))
        row3 = _OpenSpecialist(instrument="RELIANCE-2700-CE", strike=2700.0).analyze(
            snap3, cycle_id="cycle-p8-pos-c"
        )
        blocked = runner.run_from_package(
            snap3,
            _package(snap3, row3, cycle_id="cycle-p8-pos-c", digest="pkg-c"),
        )
        self.assertFalse(blocked.execution.accepted)
        self.assertIn(blocked.execution.reason, {"MAX_POSITIONS", "RISK_GUARD:positions.count"})
        self.assertEqual(len(runner.paper.positions.open_positions()), 2)
        self.assertEqual(blocked.execution.broker_order_calls, 0)

    def test_stale_quote_cannot_produce_paper_fill(self) -> None:
        """10. Stale/invalid quote cannot produce a paper fill."""
        snap = _snapshot(quotes=(_quote(quality=DataQualityStatus.STALE),))
        # Rebuild snapshot quality as STALE for gate.
        snap = build_fixture_snapshot(
            underlying="RELIANCE",
            as_of=AS_OF,
            spot=2500.0,
            option_contracts=(_quote(quality=DataQualityStatus.STALE),),
            quality=DataQualityStatus.STALE,
            notes=("stale",),
        )
        result = _runner().run_cycle(snap, cycle_id="cycle-p8-stale")
        self.assertEqual(result.decision.action, DecisionAction.NO_TRADE)
        self.assertFalse(result.execution.accepted)
        self.assertEqual(result.execution.broker_order_calls, 0)

    def test_live_trading_snapshot_rejected(self) -> None:
        """11. live_trading=True snapshot is rejected."""
        snap = _snapshot()
        object.__setattr__(snap, "live_trading", True)
        runner = _runner()
        with self.assertRaises(ValueError):
            runner.run_cycle(snap, cycle_id="cycle-p8-live")

    def test_paper_mode_false_snapshot_rejected(self) -> None:
        """12. paper_mode=False snapshot is rejected."""
        snap = _snapshot()
        object.__setattr__(snap, "paper_mode", False)
        runner = _runner()
        with self.assertRaises(ValueError):
            runner.run_cycle(snap, cycle_id="cycle-p8-nonpaper")

    def test_broker_order_calls_remain_zero(self) -> None:
        """15. broker_order_calls remains 0."""
        snap = _snapshot()
        result = _runner().run_cycle(snap, cycle_id="cycle-p8-broker-zero")
        self.assertEqual(result.execution.broker_order_calls, 0)
        self.assertEqual(result.to_dict()["execution"]["broker_order_calls"], 0)

    def test_live_paper_loop_still_importable(self) -> None:
        """Dual paths: LivePaperLoop preserved; campaign uses PaperExecutionEngine."""
        self.assertTrue(callable(LivePaperLoop))
        self.assertTrue(callable(PaperExecutionEngine))
        self.assertTrue(hasattr(CampaignRunner, "run_cycle"))
        self.assertTrue(hasattr(CampaignRunner, "run_from_package"))

    def test_no_third_executor_or_broker_in_campaign_package(self) -> None:
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
        runner_src = (ROOT / "grow" / "campaign" / "runner.py").read_text(encoding="utf-8")
        self.assertIn("PaperExecutionEngine", runner_src)
        self.assertIn("DecisionEngine", runner_src)
        self.assertIn("AnalysisOrchestrator", runner_src)
        self.assertIn("SystemClock", runner_src)
        self.assertNotIn("FrozenClock(datetime.now", runner_src)
        self.assertNotIn("class CampaignFillEngine", runner_src)


if __name__ == "__main__":
    unittest.main()
