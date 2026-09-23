"""Phase 11 — durable decision / trade replay store (provider → fill → P&L)."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from grow.campaign import (
    REPLAY_SCHEMA,
    CampaignRunner,
    TradeReplayStore,
    campaign_paper_config,
    verify_decision_replay,
    verify_paper_fill_replay,
    verify_pnl_replay,
)
from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.contract import DecisionAction
from grow.errors import GrowSafetyError
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.paper.exits import ExitReason
from grow.paper.positions import PositionState

from tests.helpers import TEST_RISK_SECRET, make_guard


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


def _snapshot(as_of=AS_OF, *, quotes=None):
    if quotes is None:
        quotes = (_quote(as_of),)
    return build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=as_of,
        spot=2500.0,
        option_contracts=tuple(quotes),
    )


class _OpenSpecialist:
    agent_name = "strategy_research"
    agent_version = "strategy_research.v2"

    def analyze(self, snapshot, *, cycle_id: str = ""):
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=("phase11",),
            calculated_metrics={
                "strategy": "trend",
                "direction": "BULLISH",
                "underlying": "RELIANCE",
                "limit_price": 100.0,
                "stop_loss": 80.0,
                "target": 140.0,
                "lots": 1,
                "quantity": 1,
                "option_type": "CE",
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
            candidate_instrument="RELIANCE-2500-CE",
            entry_reason="phase11",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
            confidence=0.5,
            cycle_id=cycle_id,
        )

    def analyze_input(self, agent_input):
        return self.analyze(agent_input.snapshot, cycle_id=agent_input.cycle_id)


def _runner(store: TradeReplayStore, *, clock=None) -> CampaignRunner:
    config = campaign_paper_config(load_config())
    config = replace(config, live_data=replace(config.live_data, session_timeout_seconds=86400))
    frozen = clock if clock is not None else FrozenClock(AS_OF)
    guard = make_guard(config, clock=frozen)
    return CampaignRunner(
        config,
        clock=frozen,
        risk_guard=guard,
        risk_secret=TEST_RISK_SECRET,
        specialists=(_OpenSpecialist(),),
        apply_campaign_defaults=False,
        replay_store=store,
    )


class Phase11TradeReplayTests(unittest.TestCase):
    def test_decision_replay(self) -> None:
        """decision replay test — engine.replay matches durable store."""
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeReplayStore(tmp)
            runner = _runner(store)
            snap = _snapshot()
            cycle = runner.run_cycle(snap, cycle_id="cycle-p11-decision")
            self.assertEqual(cycle.decision.action, DecisionAction.BUY_CE)
            self.assertEqual(len(store), 1)
            record = store.get(cycle.decision.decision_id)
            self.assertEqual(record.schema if hasattr(record, "schema") else REPLAY_SCHEMA, REPLAY_SCHEMA)
            self.assertEqual(record.to_dict()["schema"], REPLAY_SCHEMA)
            self.assertEqual(record.snapshot_id, snap.snapshot_id)
            self.assertEqual(record.cycle_id, "cycle-p11-decision")
            self.assertEqual(record.package_digest, cycle.package.package_digest)
            self.assertTrue(record.agent_versions)
            self.assertEqual(record.market_data_provider, snap.provider)
            self.assertEqual(record.contract_id, "RELIANCE-2500-CE")
            self.assertEqual(record.option_type, "CE")
            self.assertEqual(record.bid, 99.0)
            self.assertEqual(record.ask, 101.0)
            self.assertEqual(record.ltp, 100.0)
            self.assertIsNotNone(record.decision)
            self.assertEqual(record.risk_guard_result, cycle.decision.risk_guard_result)

            replayed = verify_decision_replay(
                runner.decision_engine, store, cycle.decision.decision_id
            )
            self.assertEqual(replayed.decision_id, cycle.decision.decision_id)
            self.assertEqual(replayed.action, cycle.decision.action)
            self.assertEqual(replayed.to_dict()["action"], record.decision["action"])
            # Second cycle with same package path via replay only — durable load.
            reloaded = TradeReplayStore(tmp)
            again = reloaded.get(cycle.decision.decision_id)
            self.assertEqual(again.decision["decision_id"], cycle.decision.decision_id)
            self.assertFalse(again.live_trading)
            self.assertEqual(again.broker_order_calls, 0)

    def test_paper_trade_replay(self) -> None:
        """paper trade replay test — durable fill matches live execution."""
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeReplayStore(tmp)
            runner = _runner(store)
            snap = _snapshot()
            cycle = runner.run_cycle(snap, cycle_id="cycle-p11-fill")
            self.assertTrue(cycle.execution.accepted, cycle.execution.reason)
            fill = verify_paper_fill_replay(store, cycle.decision.decision_id, cycle.execution)
            self.assertTrue(fill["accepted"])
            self.assertEqual(fill["price_source"], "ASK")
            self.assertGreaterEqual(fill["execution_price"], 101.0)
            self.assertEqual(fill["broker_order_calls"], 0)
            self.assertEqual(cycle.execution.broker_order_calls, 0)
            # Duplicate decision cannot fill again.
            again = runner.run_from_package(snap, cycle.package)
            self.assertFalse(again.execution.accepted)
            # Identical re-append is idempotent; conflicting overwrite fails closed.
            same = store.get(cycle.decision.decision_id)
            self.assertIs(store.append(same), same)
            from grow.campaign.replay import TradeReplayRecord, build_replay_record

            forged = build_replay_record(cycle, snap).to_dict()
            forged["paper_fill"] = dict(forged["paper_fill"] or {})
            forged["paper_fill"]["execution_price"] = 1.0
            with self.assertRaises(GrowSafetyError):
                store.append(TradeReplayRecord.from_dict(forged))

    def test_pnl_replay(self) -> None:
        """P&L replay test — exit attach + net == gross − costs."""
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeReplayStore(tmp)
            clock = FrozenClock(AS_OF)
            runner = _runner(store, clock=clock)
            snap = _snapshot()
            cycle = runner.run_cycle(snap, cycle_id="cycle-p11-pnl")
            self.assertTrue(cycle.execution.accepted, cycle.execution.reason)
            hit = AS_OF + timedelta(minutes=10)
            reasons = runner.on_snapshot(
                _snapshot(hit, quotes=(_quote(hit, ltp=150.0, bid=149.0, ask=151.0),))
            )
            self.assertIn(ExitReason.TAKE_PROFIT, reasons)
            closed = runner.paper.positions.all()[0]
            self.assertEqual(closed.state, PositionState.CLOSED)
            record = store.get(cycle.decision.decision_id)
            self.assertIsNotNone(record.exit)
            self.assertIsNotNone(record.pnl)
            self.assertEqual(record.exit["exit_reason"], ExitReason.TAKE_PROFIT)
            pnl = verify_pnl_replay(store, cycle.decision.decision_id)
            self.assertTrue(pnl["net_equals_gross_minus_costs"])
            self.assertEqual(runner.paper.broker_order_calls, 0)
            # Cross-process: reload store and verify P&L still holds.
            reloaded = TradeReplayStore(tmp)
            verify_pnl_replay(reloaded, cycle.decision.decision_id)
            # Idempotent re-attach of the same exit is allowed.
            reloaded.attach_exit(cycle.decision.decision_id, position=closed)
            # Conflicting exit rewrite fails closed.
            forged = replace(closed, realized_pnl=closed.realized_pnl + 99.0)
            with self.assertRaises(GrowSafetyError):
                reloaded.attach_exit(cycle.decision.decision_id, position=forged)

    def test_required_persist_fields_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradeReplayStore(tmp)
            runner = _runner(store)
            cycle = runner.run_cycle(_snapshot(), cycle_id="cycle-p11-fields")
            payload = store.get(cycle.decision.decision_id).to_dict()
            required = {
                "snapshot_id",
                "cycle_id",
                "decision_id",
                "package_digest",
                "agent_versions",
                "market_data_provider",
                "contract_id",
                "expiry",
                "strike",
                "option_type",
                "bid",
                "ask",
                "ltp",
                "timestamp",
                "decision",
                "risk_guard_result",
                "paper_fill",
                "exit",
                "pnl",
            }
            for key in required:
                self.assertIn(key, payload)
            self.assertTrue(payload["paper_mode"])
            self.assertFalse(payload["live_trading"])
            self.assertFalse(payload["broker_order_path"])


if __name__ == "__main__":
    unittest.main()
