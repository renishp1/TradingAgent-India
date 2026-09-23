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
    TradeReplayRecord,
    TradeReplayStore,
    campaign_paper_config,
    verify_decision_replay,
    verify_paper_fill_replay,
    verify_pnl_replay,
)
from grow.campaign.replay import build_replay_record
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
            again_record = store.append(same)
            self.assertEqual(again_record.to_dict(), same.to_dict())
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


def _base_replay_payload(decision_id: str = "dec-concurrent-1") -> dict:
    return {
        "schema": REPLAY_SCHEMA,
        "snapshot_id": "snap-concurrent",
        "cycle_id": "cycle-concurrent",
        "decision_id": decision_id,
        "package_digest": "pkg-concurrent",
        "agent_versions": ["strategy_research@strategy_research.v2"],
        "market_data_provider": "grow.fixture.agent.v1",
        "contract_id": "RELIANCE-2500-CE",
        "expiry": EXPIRY.isoformat(),
        "strike": 2500.0,
        "option_type": "CE",
        "bid": 99.0,
        "ask": 101.0,
        "ltp": 100.0,
        "timestamp": AS_OF.isoformat(),
        "decision": {
            "decision_id": decision_id,
            "action": "BUY_CE",
            "status": "CANDIDATE",
            "risk_guard_result": "APPROVED",
            "snapshot_id": "snap-concurrent",
            "analysis_cycle_id": "cycle-concurrent",
        },
        "risk_guard_result": "APPROVED",
        "paper_fill": {
            "accepted": True,
            "reason": "FILLED",
            "paper_order_id": "po-1",
            "position_id": "pos-1",
            "execution_price": 101.0,
            "price_source": "ASK",
            "broker_order_calls": 0,
            "side": "BUY",
        },
        "exit": None,
        "pnl": None,
        "paper_mode": True,
        "live_trading": False,
        "broker_order_path": False,
        "broker_order_calls": 0,
    }


def _mp_append_worker(root: str, payload: dict, ready, go, results, index: int) -> None:
    """Separate-process append worker. ready/go are multiprocessing.Event."""
    from grow.campaign.replay import TradeReplayRecord, TradeReplayStore
    from grow.errors import GrowSafetyError

    store = TradeReplayStore(root)
    ready.set()
    go.wait(timeout=30)
    try:
        store.append(TradeReplayRecord.from_dict(payload))
        results[index] = ("ok", None)
    except GrowSafetyError as exc:
        results[index] = ("err", str(exc))
    except Exception as exc:  # noqa: BLE001 — surface unexpected failures to parent
        results[index] = ("boom", f"{type(exc).__name__}:{exc}")


def _mp_exit_worker(root: str, decision_id: str, exit_row: dict, pnl_row: dict, ready, go, results, index: int) -> None:
    from grow.campaign.replay import TradeReplayStore
    from grow.errors import GrowSafetyError
    from grow.paper.positions import PaperPosition, PositionState

    store = TradeReplayStore(root)
    position = PaperPosition(
        position_id="pos-1",
        session_id="session-concurrent",
        candidate_id=None,
        contract_id="RELIANCE-2500-CE",
        underlying="RELIANCE",
        expiry=EXPIRY,
        strike=2500.0,
        option_type="CE",
        provider_id="grow.fixture.agent.v1",
        lot_size=1,
        lots=1,
        quantity=1,
        entry_price=101.0,
        opened_at=AS_OF,
        stop_loss_price=80.0,
        take_profit_price=140.0,
        state=PositionState.CLOSED,
        current_price=float(exit_row["exit_price"]),
        realized_pnl=float(pnl_row["realized_pnl"]),
        realized_gross=float(pnl_row["realized_gross"]),
        total_costs=float(pnl_row["total_costs"]),
        exit_reason=str(exit_row["exit_reason"]),
        closed_at=AS_OF + timedelta(minutes=10),
        close_snapshot_id="snap-exit",
    )
    ready.set()
    go.wait(timeout=30)
    try:
        store.attach_exit(decision_id, position=position)
        results[index] = ("ok", None)
    except GrowSafetyError as exc:
        results[index] = ("err", str(exc))
    except Exception as exc:  # noqa: BLE001
        results[index] = ("boom", f"{type(exc).__name__}:{exc}")


class Phase11ConcurrentReplayTests(unittest.TestCase):
    def test_concurrent_conflicting_initial_append(self) -> None:
        """Two store processes race on the same decision_id; one wins, one raises."""
        import multiprocessing as mp

        with tempfile.TemporaryDirectory() as tmp:
            decision_id = "dec-concurrent-append"
            left = _base_replay_payload(decision_id)
            right = _base_replay_payload(decision_id)
            right["paper_fill"] = dict(right["paper_fill"])
            right["paper_fill"]["execution_price"] = 199.0
            ctx = mp.get_context("spawn")
            ready_a = ctx.Event()
            ready_b = ctx.Event()
            go = ctx.Event()
            results = ctx.Manager().list([None, None])
            procs = [
                ctx.Process(target=_mp_append_worker, args=(tmp, left, ready_a, go, results, 0)),
                ctx.Process(target=_mp_append_worker, args=(tmp, right, ready_b, go, results, 1)),
            ]
            for proc in procs:
                proc.start()
            self.assertTrue(ready_a.wait(30) and ready_b.wait(30))
            go.set()
            for proc in procs:
                proc.join(30)
                self.assertFalse(proc.is_alive())
                self.assertEqual(proc.exitcode, 0)
            outcomes = list(results)
            self.assertEqual({row[0] for row in outcomes}, {"ok", "err"})
            self.assertTrue(any("append-only" in (row[1] or "") or "refusing overwrite" in (row[1] or "") for row in outcomes if row[0] == "err"))
            reloaded = TradeReplayStore(tmp)
            durable = reloaded.get(decision_id).to_dict()
            price = durable["paper_fill"]["execution_price"]
            self.assertIn(price, {101.0, 199.0})
            self.assertEqual(durable["broker_order_calls"], 0)
            self.assertEqual(durable["paper_fill"]["broker_order_calls"], 0)
            # Internally consistent: only one fill price on disk.
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 1)

    def test_concurrent_identical_append_is_idempotent(self) -> None:
        import multiprocessing as mp

        with tempfile.TemporaryDirectory() as tmp:
            payload = _base_replay_payload("dec-concurrent-same")
            ctx = mp.get_context("spawn")
            ready_a = ctx.Event()
            ready_b = ctx.Event()
            go = ctx.Event()
            results = ctx.Manager().list([None, None])
            procs = [
                ctx.Process(target=_mp_append_worker, args=(tmp, payload, ready_a, go, results, 0)),
                ctx.Process(target=_mp_append_worker, args=(tmp, payload, ready_b, go, results, 1)),
            ]
            for proc in procs:
                proc.start()
            self.assertTrue(ready_a.wait(30) and ready_b.wait(30))
            go.set()
            for proc in procs:
                proc.join(30)
                self.assertEqual(proc.exitcode, 0)
            self.assertEqual(list(results), [("ok", None), ("ok", None)])
            durable = TradeReplayStore(tmp).get("dec-concurrent-same")
            self.assertEqual(durable.paper_fill["execution_price"], 101.0)
            self.assertEqual(durable.broker_order_calls, 0)

    def test_concurrent_conflicting_exit_attachment(self) -> None:
        import multiprocessing as mp

        with tempfile.TemporaryDirectory() as tmp:
            decision_id = "dec-concurrent-exit"
            base = _base_replay_payload(decision_id)
            TradeReplayStore(tmp).append(TradeReplayRecord.from_dict(base))
            exit_a = {
                "position_id": "pos-1",
                "contract_id": "RELIANCE-2500-CE",
                "exit_reason": "TAKE_PROFIT",
                "exit_price": 150.0,
                "closed_at": (AS_OF + timedelta(minutes=10)).isoformat(),
                "close_snapshot_id": "snap-exit",
                "side": "SELL",
            }
            pnl_a = {
                "realized_pnl": 40.0,
                "realized_gross": 49.0,
                "total_costs": 9.0,
                "entry_price": 101.0,
                "exit_price": 150.0,
                "quantity": 1,
                "identity": "net == gross - costs",
                "net_equals_gross_minus_costs": True,
            }
            exit_b = dict(exit_a)
            exit_b["exit_price"] = 90.0
            exit_b["exit_reason"] = "STOP_LOSS"
            pnl_b = dict(pnl_a)
            pnl_b["realized_pnl"] = -20.0
            pnl_b["realized_gross"] = -11.0
            pnl_b["exit_price"] = 90.0

            ctx = mp.get_context("spawn")
            ready_a = ctx.Event()
            ready_b = ctx.Event()
            go = ctx.Event()
            results = ctx.Manager().list([None, None])
            procs = [
                ctx.Process(
                    target=_mp_exit_worker,
                    args=(tmp, decision_id, exit_a, pnl_a, ready_a, go, results, 0),
                ),
                ctx.Process(
                    target=_mp_exit_worker,
                    args=(tmp, decision_id, exit_b, pnl_b, ready_b, go, results, 1),
                ),
            ]
            for proc in procs:
                proc.start()
            self.assertTrue(ready_a.wait(30) and ready_b.wait(30))
            go.set()
            for proc in procs:
                proc.join(30)
                self.assertEqual(proc.exitcode, 0)
            outcomes = list(results)
            self.assertEqual({row[0] for row in outcomes}, {"ok", "err"})
            durable = TradeReplayStore(tmp).get(decision_id)
            self.assertIsNotNone(durable.exit)
            self.assertIsNotNone(durable.pnl)
            self.assertIn(durable.exit["exit_reason"], {"TAKE_PROFIT", "STOP_LOSS"})
            self.assertEqual(durable.broker_order_calls, 0)
            # Identity still holds on the durable winner.
            verify_pnl_replay(TradeReplayStore(tmp), decision_id)

    def test_concurrent_identical_exit_attachment_is_idempotent(self) -> None:
        import multiprocessing as mp

        with tempfile.TemporaryDirectory() as tmp:
            decision_id = "dec-concurrent-exit-same"
            base = _base_replay_payload(decision_id)
            from grow.campaign.replay import TradeReplayRecord

            TradeReplayStore(tmp).append(TradeReplayRecord.from_dict(base))
            exit_row = {
                "position_id": "pos-1",
                "contract_id": "RELIANCE-2500-CE",
                "exit_reason": "TAKE_PROFIT",
                "exit_price": 150.0,
                "closed_at": (AS_OF + timedelta(minutes=10)).isoformat(),
                "close_snapshot_id": "snap-exit",
                "side": "SELL",
            }
            pnl_row = {
                "realized_pnl": 40.0,
                "realized_gross": 49.0,
                "total_costs": 9.0,
                "entry_price": 101.0,
                "exit_price": 150.0,
                "quantity": 1,
                "identity": "net == gross - costs",
                "net_equals_gross_minus_costs": True,
            }
            ctx = mp.get_context("spawn")
            ready_a = ctx.Event()
            ready_b = ctx.Event()
            go = ctx.Event()
            results = ctx.Manager().list([None, None])
            procs = [
                ctx.Process(
                    target=_mp_exit_worker,
                    args=(tmp, decision_id, exit_row, pnl_row, ready_a, go, results, 0),
                ),
                ctx.Process(
                    target=_mp_exit_worker,
                    args=(tmp, decision_id, exit_row, pnl_row, ready_b, go, results, 1),
                ),
            ]
            for proc in procs:
                proc.start()
            self.assertTrue(ready_a.wait(30) and ready_b.wait(30))
            go.set()
            for proc in procs:
                proc.join(30)
                self.assertEqual(proc.exitcode, 0)
            self.assertEqual(list(results), [("ok", None), ("ok", None)])
            durable = TradeReplayStore(tmp).get(decision_id)
            self.assertEqual(durable.exit["exit_reason"], "TAKE_PROFIT")
            self.assertEqual(durable.pnl["realized_pnl"], 40.0)
            self.assertEqual(durable.broker_order_calls, 0)


if __name__ == "__main__":
    unittest.main()
