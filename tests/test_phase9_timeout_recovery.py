"""Phase 9 — timeout / failure recovery + durable cross-process checkpoints."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from grow.campaign import CampaignRunner, campaign_paper_config
from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.contract import DecisionAction, IntegratedDecisionStatus
from grow.decision.integration.integrator import DecisionIntegrator
from grow.errors import GrowSafetyError
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.orchestration.models import AggregateAnalysisPackage
from grow.paper.checkpoint import (
    CHECKPOINT_SCHEMA,
    PaperCheckpointStore,
    load_paper_checkpoint,
    restore_paper_engine,
    save_paper_checkpoint,
    wrap_checkpoint,
)
from grow.paper.engine import PaperExecutionEngine
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


def _snapshot(as_of=AS_OF, *, quotes=None, quality=DataQualityStatus.OK):
    if quotes is None:
        quotes = (_quote(as_of),)
    return build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=as_of,
        spot=2500.0,
        option_contracts=tuple(quotes),
        quality=quality,
        notes=("phase9",) if quality is not DataQualityStatus.OK else (),
    )


def _result(snapshot, *, instrument="RELIANCE-2500-CE", cycle_id="cycle-p9"):
    return AgentResult(
        agent_name="strategy_research",
        agent_version="strategy_research.v2",
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        decision_timestamp=snapshot.decision_timestamp,
        status=AgentStatus.PASS,
        observations=("phase9",),
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
        candidate_instrument=instrument,
        entry_reason="phase9",
        invalidation_reason=None,
        risk_flags=(),
        missing_data=(),
        confidence=0.5,
        cycle_id=cycle_id,
    )


def _package(snapshot, output, *, cycle_id="cycle-p9"):
    return AggregateAnalysisPackage(
        cycle_id=cycle_id,
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        as_of=snapshot.decision_timestamp,
        agent_outputs=(output,),
        rejected_outputs=(),
        dispatch_records=(),
        conflicts=(),
        supporting_evidence=("pkg",),
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
        cycle_summary="phase9",
        package_digest=f"pkg-{cycle_id}",
    )


def _config(*, timeout_seconds=30):
    config = campaign_paper_config(load_config())
    return replace(
        config,
        live_data=replace(config.live_data, session_timeout_seconds=timeout_seconds),
    )


def _approved(config, clock, snapshot, *, cycle_id="cycle-p9"):
    output = _result(snapshot, cycle_id=cycle_id)
    package = _package(snapshot, output, cycle_id=cycle_id)
    integrator = DecisionIntegrator(config, risk_guard=make_guard(config, clock=clock))
    decision = integrator.integrate(snapshot=snapshot, package=package)
    return decision, package


class _OpenSpecialist:
    agent_name = "strategy_research"
    agent_version = "strategy_research.v2"

    def analyze(self, snapshot, *, cycle_id: str = ""):
        return _result(snapshot, cycle_id=cycle_id or "cycle-p9")

    def analyze_input(self, agent_input):
        return self.analyze(agent_input.snapshot, cycle_id=agent_input.cycle_id)


class Phase9TimeoutSequenceTests(unittest.TestCase):
    def test_full_timeout_recovery_sequence(self) -> None:
        """Open → timeout → stale/unresolved → fresh close once → halted for entries."""
        config = _config()
        clock = FrozenClock(AS_OF)
        engine = PaperExecutionEngine(config, clock=clock, risk_secret=TEST_RISK_SECRET)
        snap = _snapshot()
        decision, package = _approved(config, clock, snap)
        self.assertTrue(engine.execute(decision, snap, package=package).accepted)

        clock.advance(timedelta(seconds=31))
        stale = _snapshot(AS_OF + timedelta(minutes=1), quality=DataQualityStatus.STALE)
        first = engine.on_snapshot(stale)
        self.assertIn("SESSION_TIMEOUT", first)
        self.assertIn("DATA_STALE", first)
        open_pos = engine.positions.open_positions()[0]
        self.assertEqual(open_pos.state, PositionState.OPEN)
        self.assertTrue(engine.positions.unresolved_close)
        self.assertTrue(engine.positions.halted)
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
        # Conservative exits use BID (possibly with slippage) — never fabricate a price.
        self.assertIsNotNone(closed.current_price)
        self.assertLessEqual(closed.current_price, 105.0)
        self.assertGreaterEqual(closed.current_price, 104.0)

        later = recover_at + timedelta(minutes=1)
        blocked_snap = _snapshot(later, quotes=(_quote(later, ltp=104.0, bid=104.0, ask=105.0),))
        blocked_decision, blocked_pkg = _approved(config, clock, blocked_snap, cycle_id="cycle-p9-block")
        blocked = engine.execute(blocked_decision, blocked_snap, package=blocked_pkg)
        self.assertFalse(blocked.accepted)
        self.assertIn(blocked.reason, {"SESSION_TIMEOUT", "NEW_ENTRIES_BLOCKED"})
        self.assertEqual(engine.broker_order_calls, 0)

    def test_no_fabricated_close_on_stale_timeout(self) -> None:
        config = _config()
        clock = FrozenClock(AS_OF)
        engine = PaperExecutionEngine(config, clock=clock, risk_secret=TEST_RISK_SECRET)
        snap = _snapshot()
        decision, package = _approved(config, clock, snap)
        self.assertTrue(engine.execute(decision, snap, package=package).accepted)
        entry_price = engine.positions.open_positions()[0].entry_price
        clock.advance(timedelta(seconds=31))
        engine.on_snapshot(_snapshot(AS_OF + timedelta(minutes=1), quality=DataQualityStatus.STALE))
        open_pos = engine.positions.open_positions()[0]
        self.assertEqual(open_pos.state, PositionState.OPEN)
        self.assertEqual(open_pos.entry_price, entry_price)
        self.assertIsNone(open_pos.closed_at)
        sell_fills = [row for row in engine.journal.records if row.kind == "FILL" and row.payload.get("side") == "SELL"]
        self.assertEqual(sell_fills, [])

    def test_no_duplicate_close_after_recovery(self) -> None:
        config = _config()
        clock = FrozenClock(AS_OF)
        engine = PaperExecutionEngine(config, clock=clock, risk_secret=TEST_RISK_SECRET)
        snap = _snapshot()
        decision, package = _approved(config, clock, snap, cycle_id="cycle-p9-dup")
        self.assertTrue(engine.execute(decision, snap, package=package).accepted)
        clock.advance(timedelta(seconds=31))
        engine.on_snapshot(_snapshot(AS_OF + timedelta(minutes=1), quality=DataQualityStatus.STALE))
        recover_at = AS_OF + timedelta(minutes=5)
        engine.on_snapshot(_snapshot(recover_at, quotes=(_quote(recover_at, ltp=105.0, bid=105.0, ask=106.0),)))
        fills = len(engine.ledger.book.fills)
        engine.on_snapshot(
            _snapshot(
                recover_at + timedelta(minutes=1),
                quotes=(_quote(recover_at + timedelta(minutes=1), ltp=104.0, bid=104.0, ask=105.0),),
            )
        )
        self.assertEqual(len(engine.ledger.book.fills), fills)
        self.assertEqual(engine.positions.summary().closes, 1)
        recovery_rows = [
            row for row in engine.journal.records if row.payload.get("reason") == "TIMEOUT_RECOVERY_CLOSED"
        ]
        self.assertEqual(len(recovery_rows), 1)


class Phase9DurableCheckpointTests(unittest.TestCase):
    def test_cross_process_restart_preserves_open_position(self) -> None:
        config = _config(timeout_seconds=86400)
        clock = FrozenClock(AS_OF)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.json"
            engine = PaperExecutionEngine(
                config,
                clock=clock,
                risk_secret=TEST_RISK_SECRET,
                checkpoint_path=path,
            )
            snap = _snapshot()
            decision, package = _approved(config, clock, snap)
            self.assertTrue(engine.execute(decision, snap, package=package).accepted)
            self.assertTrue(path.is_file())
            first_journal = engine.journal.to_list()
            position_id = engine.positions.open_positions()[0].position_id
            cash = engine.ledger.book.cash

            # Simulate a new process: drop the live engine and restore from disk only.
            del engine
            restored = restore_paper_engine(config, path, clock=clock, risk_secret=TEST_RISK_SECRET)
            self.assertEqual(restored.journal.to_list(), first_journal)
            self.assertEqual(restored.positions.open_positions()[0].position_id, position_id)
            self.assertEqual(restored.ledger.book.cash, cash)
            again = restored.execute(decision, snap, package=package)
            self.assertFalse(again.accepted)
            self.assertEqual(again.reason, "DUPLICATE_DECISION")
            self.assertEqual(len(restored.positions.open_positions()), 1)
            with self.assertRaises(GrowSafetyError):
                restored.journal.rewrite(0, {})
            with self.assertRaises(GrowSafetyError):
                restored.journal.delete(0)

    def test_timeout_unresolved_survives_disk_restart_then_recovers(self) -> None:
        config = _config()
        clock = FrozenClock(AS_OF)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timeout.json"
            engine = PaperExecutionEngine(
                config,
                clock=clock,
                risk_secret=TEST_RISK_SECRET,
                checkpoint_path=path,
            )
            snap = _snapshot()
            decision, package = _approved(config, clock, snap, cycle_id="cycle-p9-restart-to")
            self.assertTrue(engine.execute(decision, snap, package=package).accepted)
            clock.advance(timedelta(seconds=31))
            engine.on_snapshot(_snapshot(AS_OF + timedelta(minutes=1), quality=DataQualityStatus.STALE))
            self.assertTrue(engine.positions.unresolved_close)
            self.assertTrue(path.is_file())
            payload = load_paper_checkpoint(path)
            self.assertEqual(payload["schema"], CHECKPOINT_SCHEMA)
            self.assertTrue(payload["engine"]["registry"]["unresolved_close"])
            self.assertTrue(payload["engine"]["registry"]["halted"])

            restored = restore_paper_engine(config, path, clock=clock, risk_secret=TEST_RISK_SECRET)
            self.assertTrue(restored.positions.unresolved_close)
            self.assertTrue(restored.positions.halted)
            self.assertEqual(len(restored.positions.open_positions()), 1)
            recover_at = AS_OF + timedelta(minutes=5)
            recovered = restored.on_snapshot(
                _snapshot(recover_at, quotes=(_quote(recover_at, ltp=105.0, bid=105.0, ask=106.0),))
            )
            self.assertIn(ExitReason.SESSION_TIMEOUT, recovered)
            self.assertEqual(restored.positions.open_positions(), ())
            self.assertFalse(restored.positions.unresolved_close)
            self.assertTrue(restored.positions.halted)
            recovery_rows = [
                row
                for row in restored.journal.records
                if row.payload.get("reason") == "TIMEOUT_RECOVERY_CLOSED"
            ]
            self.assertEqual(len(recovery_rows), 1)
            self.assertEqual(restored.broker_order_calls, 0)

    def test_journal_recovery_is_append_only_after_restore(self) -> None:
        config = _config(timeout_seconds=86400)
        clock = FrozenClock(AS_OF)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.json"
            store = PaperCheckpointStore(path)
            engine = PaperExecutionEngine(config, clock=clock, risk_secret=TEST_RISK_SECRET)
            snap = _snapshot()
            decision, package = _approved(config, clock, snap, cycle_id="cycle-p9-journal")
            self.assertTrue(engine.execute(decision, snap, package=package).accepted)
            store.save(engine)
            first = engine.journal.records[0].to_dict()
            restored = store.restore(config, clock=clock, risk_secret=TEST_RISK_SECRET)
            self.assertEqual(restored.journal.records[0].to_dict(), first)
            later = AS_OF + timedelta(minutes=5)
            restored.on_snapshot(_snapshot(later, quotes=(_quote(later, ltp=102.0, bid=102.0, ask=103.0),)))
            self.assertEqual(restored.journal.records[0].to_dict(), first)
            self.assertGreater(len(restored.journal.records), len(engine.journal.records))
            with self.assertRaises(GrowSafetyError):
                restored.journal.load(store.load()["engine"]["journal"])


    def test_stale_snapshot_persists_before_return_for_cross_process_recovery(self) -> None:
        """Stale on_snapshot must checkpoint before return; restore sees the mutation."""
        config = _config()
        clock = FrozenClock(AS_OF)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stale-persist.json"
            engine = PaperExecutionEngine(
                config,
                clock=clock,
                risk_secret=TEST_RISK_SECRET,
                checkpoint_path=path,
            )
            snap = _snapshot()
            decision, package = _approved(config, clock, snap, cycle_id="cycle-p9-stale-ckpt")
            self.assertTrue(engine.execute(decision, snap, package=package).accepted)
            self.assertTrue(path.is_file())
            before = path.read_bytes()
            journal_len_before = len(engine.journal.records)

            clock.advance(timedelta(seconds=31))
            stale_reasons = engine.on_snapshot(
                _snapshot(AS_OF + timedelta(minutes=1), quality=DataQualityStatus.STALE)
            )
            self.assertIn("SESSION_TIMEOUT", stale_reasons)
            self.assertIn("DATA_STALE", stale_reasons)
            self.assertTrue(engine.positions.unresolved_close)
            self.assertTrue(engine.positions.halted)
            self.assertGreater(len(engine.journal.records), journal_len_before)

            after = path.read_bytes()
            self.assertNotEqual(after, before, "checkpoint must change after stale mutation")
            on_disk = load_paper_checkpoint(path)
            self.assertTrue(on_disk["engine"]["registry"]["unresolved_close"])
            self.assertTrue(on_disk["engine"]["registry"]["halted"])
            self.assertGreater(len(on_disk["engine"]["journal"]), journal_len_before)
            stale_rows = [
                row
                for row in on_disk["engine"]["journal"]
                if row.get("payload", {}).get("reason") == "DATA_STALE"
            ]
            self.assertTrue(stale_rows)

            # Fresh process: restore only from disk.
            del engine
            restored = restore_paper_engine(config, path, clock=clock, risk_secret=TEST_RISK_SECRET)
            self.assertTrue(restored.positions.unresolved_close)
            self.assertTrue(restored.positions.halted)
            self.assertEqual(len(restored.positions.open_positions()), 1)
            self.assertEqual(len(restored.journal.records), len(on_disk["engine"]["journal"]))

            recover_at = AS_OF + timedelta(minutes=5)
            recovered = restored.on_snapshot(
                _snapshot(recover_at, quotes=(_quote(recover_at, ltp=105.0, bid=105.0, ask=106.0),))
            )
            self.assertIn(ExitReason.SESSION_TIMEOUT, recovered)
            self.assertEqual(restored.positions.open_positions(), ())
            self.assertFalse(restored.positions.unresolved_close)
            self.assertTrue(restored.positions.halted)
            self.assertEqual(restored.positions.summary().closes, 1)
            recovery_rows = [
                row
                for row in restored.journal.records
                if row.payload.get("reason") == "TIMEOUT_RECOVERY_CLOSED"
            ]
            self.assertEqual(len(recovery_rows), 1)
            sell_fills = [
                row
                for row in restored.journal.records
                if row.kind == "FILL" and row.payload.get("side") == "SELL"
            ]
            self.assertEqual(len(sell_fills), 1)

            # No duplicate close on a subsequent fresh quote.
            fills = len(restored.ledger.book.fills)
            later = recover_at + timedelta(minutes=1)
            restored.on_snapshot(
                _snapshot(later, quotes=(_quote(later, ltp=104.0, bid=104.0, ask=105.0),))
            )
            self.assertEqual(len(restored.ledger.book.fills), fills)
            self.assertEqual(restored.positions.summary().closes, 1)
            self.assertEqual(
                len(
                    [
                        row
                        for row in restored.journal.records
                        if row.payload.get("reason") == "TIMEOUT_RECOVERY_CLOSED"
                    ]
                ),
                1,
            )
            self.assertEqual(restored.broker_order_calls, 0)


class Phase9PartialFailureTests(unittest.TestCase):
    def test_corrupt_checkpoint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaises(GrowSafetyError):
                load_paper_checkpoint(path)

    def test_truncated_checkpoint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trunc.json"
            path.write_text('{"schema": "paper.checkpoint.v1", "engine": {', encoding="utf-8")
            with self.assertRaises(GrowSafetyError):
                load_paper_checkpoint(path)

    def test_empty_checkpoint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.json"
            path.write_text("\n", encoding="utf-8")
            with self.assertRaises(GrowSafetyError):
                load_paper_checkpoint(path)

    def test_missing_engine_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": CHECKPOINT_SCHEMA,
                        "paper_mode": True,
                        "live_trading": False,
                        "broker_order_path": False,
                        "engine": {"session_id": "x"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(GrowSafetyError):
                load_paper_checkpoint(path)

    def test_live_trading_checkpoint_rejected(self) -> None:
        with self.assertRaises(GrowSafetyError):
            wrap_checkpoint({"paper_mode": True, "live_trading": True, "journal": []})

    def test_tmp_crash_leaves_previous_checkpoint_intact(self) -> None:
        config = _config(timeout_seconds=86400)
        clock = FrozenClock(AS_OF)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stable.json"
            engine = PaperExecutionEngine(
                config,
                clock=clock,
                risk_secret=TEST_RISK_SECRET,
                checkpoint_path=path,
            )
            snap = _snapshot()
            decision, package = _approved(config, clock, snap, cycle_id="cycle-p9-stable")
            self.assertTrue(engine.execute(decision, snap, package=package).accepted)
            good = path.read_text(encoding="utf-8")
            # Simulate a crashed mid-write: leftover .tmp must not replace the good file.
            crash_tmp = path.parent / f".{path.name}.crash.tmp"
            crash_tmp.write_text("{incomplete", encoding="utf-8")
            restored = restore_paper_engine(config, path, clock=clock, risk_secret=TEST_RISK_SECRET)
            self.assertEqual(path.read_text(encoding="utf-8"), good)
            self.assertEqual(len(restored.positions.open_positions()), 1)
            self.assertTrue(crash_tmp.exists())


class Phase9CampaignCheckpointTests(unittest.TestCase):
    def test_campaign_runner_restores_from_checkpoint(self) -> None:
        config = _config(timeout_seconds=86400)
        clock = FrozenClock(AS_OF)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.json"
            runner = CampaignRunner(
                config,
                clock=clock,
                risk_secret=TEST_RISK_SECRET,
                specialists=(_OpenSpecialist(),),
                apply_campaign_defaults=False,
                checkpoint_path=path,
            )
            result = runner.run_cycle(_snapshot(), cycle_id="cycle-p9-campaign")
            self.assertTrue(result.execution.accepted, result.execution.reason)
            self.assertEqual(result.decision.action, DecisionAction.BUY_CE)
            self.assertEqual(result.execution.broker_order_calls, 0)
            self.assertTrue(path.is_file())

            restarted = CampaignRunner.from_checkpoint(
                path,
                config,
                clock=clock,
                risk_secret=TEST_RISK_SECRET,
                specialists=(_OpenSpecialist(),),
                apply_campaign_defaults=False,
            )
            self.assertEqual(len(restarted.paper.positions.open_positions()), 1)
            self.assertIs(restarted.guard.clock, restarted.clock)
            self.assertIs(restarted.paper.clock, restarted.clock)
            blocked = restarted.run_cycle(_snapshot(AS_OF + timedelta(minutes=1)), cycle_id="cycle-p9-again")
            # Fresh snapshot/decision ids differ, but max positions / open inventory may fill or
            # duplicate-protect depending on decision id — either way no broker path.
            self.assertEqual(blocked.execution.broker_order_calls, 0)
            self.assertLessEqual(len(restarted.paper.positions.open_positions()), 2)

    def test_campaign_timeout_recovery_across_restart(self) -> None:
        config = _config()
        clock = FrozenClock(AS_OF)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign-to.json"
            runner = CampaignRunner(
                config,
                clock=clock,
                risk_secret=TEST_RISK_SECRET,
                specialists=(_OpenSpecialist(),),
                apply_campaign_defaults=False,
                checkpoint_path=path,
            )
            opened = runner.run_cycle(_snapshot(), cycle_id="cycle-p9-camp-to")
            self.assertTrue(opened.execution.accepted, opened.execution.reason)
            clock.advance(timedelta(seconds=31))
            runner.on_snapshot(_snapshot(AS_OF + timedelta(minutes=1), quality=DataQualityStatus.STALE))
            self.assertTrue(runner.paper.positions.unresolved_close)

            restarted = CampaignRunner.from_checkpoint(
                path,
                config,
                clock=clock,
                risk_secret=TEST_RISK_SECRET,
                specialists=(),
                apply_campaign_defaults=False,
            )
            recover_at = AS_OF + timedelta(minutes=5)
            recovered = restarted.on_snapshot(
                _snapshot(recover_at, quotes=(_quote(recover_at, ltp=105.0, bid=105.0, ask=106.0),))
            )
            self.assertIn(ExitReason.SESSION_TIMEOUT, recovered)
            self.assertEqual(restarted.paper.positions.open_positions(), ())
            self.assertFalse(restarted.paper.positions.unresolved_close)
            self.assertTrue(restarted.paper.positions.halted)
            self.assertEqual(restarted.paper.broker_order_calls, 0)


if __name__ == "__main__":
    unittest.main()
