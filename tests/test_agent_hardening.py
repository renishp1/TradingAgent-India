"""Hardening: specialist timeout, concurrency, stable cycle ID, second-pass opt-in."""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime

from grow.agents.orchestrator import AgentCycleOrchestrator, stable_cycle_id
from grow.clock import IST
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot


class _FastAgent:
    def __init__(self, name: str, started: list[str], barrier: threading.Barrier | None = None) -> None:
        self.agent_name = name
        self.agent_version = f"{name}.v1"
        self._started = started
        self._barrier = barrier

    def analyze(self, snapshot):
        self._started.append(self.agent_name)
        if self._barrier is not None:
            self._barrier.wait(timeout=2.0)
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=("ok",),
            metrics_used=(),
            candidate_action=CandidateAction.ABSTAIN,
            candidate_instrument=None,
            entry_reason="fast",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
        )


class _SlowAgent:
    agent_name = "slow"
    agent_version = "slow.v1"

    def __init__(self, delay: float = 5.0) -> None:
        self.delay = delay

    def analyze(self, snapshot):
        time.sleep(self.delay)
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=("too late",),
            metrics_used=(),
            candidate_action=CandidateAction.PAPER_OPEN,
            candidate_instrument="NIFTY-25000-CE",
            entry_reason="should-not-apply",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
        )


class _OpenAgent:
    agent_name = "force_open"
    agent_version = "force_open.v1"

    def analyze(self, snapshot):
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=("force open",),
            metrics_used=(),
            candidate_action=CandidateAction.PAPER_OPEN,
            candidate_instrument="NIFTY-25000-CE",
            entry_reason="test",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
        )


class _OrderedAgent:
    def __init__(self, name: str, hold: threading.Event, release: threading.Event) -> None:
        self.agent_name = name
        self.agent_version = f"{name}.v1"
        self._hold = hold
        self._release = release

    def analyze(self, snapshot):
        self._hold.set()
        self._release.wait(timeout=2.0)
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=(self.agent_name,),
            metrics_used=(),
            candidate_action=CandidateAction.ABSTAIN,
            candidate_instrument=None,
            entry_reason=None,
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
        )


def _snapshot():
    as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
    option = OptionQuoteView(
        underlying="NIFTY",
        expiry=as_of.date(),
        strike=25000.0,
        option_type="CE",
        ltp=120.0,
        bid=119.0,
        ask=121.0,
        open_interest=100,
        volume=10,
        quote_timestamp=as_of,
        quote_age_seconds=0.0,
        provider_contract_id="ce-1",
        quality=DataQualityStatus.OK,
    )
    return build_fixture_snapshot(
        underlying="NIFTY",
        as_of=as_of,
        spot=25000.0,
        option_contracts=(option,),
    )


class HardeningOrchestratorTests(unittest.TestCase):
    def test_specialist_timeout_produces_error_and_fail_closed(self) -> None:
        orch = AgentCycleOrchestrator(
            specialists=(_SlowAgent(delay=5.0),),
            agent_timeout_seconds=0.2,
        )
        started = time.monotonic()
        decision = orch.run(_snapshot())
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        result = decision.agent_results[0]
        self.assertEqual(result.status, AgentStatus.ERROR)
        self.assertIn("AGENT_TIMEOUT", result.risk_flags)
        self.assertEqual(result.candidate_action, CandidateAction.NONE)
        self.assertEqual(decision.proposed_action, CandidateAction.NONE)
        self.assertEqual(decision.decision, "NO_TRADE")
        self.assertIn("SPECIALIST_ERROR", decision.reason)

    def test_timeout_does_not_create_actionable_recommendation(self) -> None:
        orch = AgentCycleOrchestrator(
            specialists=(_OpenAgent(), _SlowAgent(delay=5.0)),
            agent_timeout_seconds=0.2,
        )
        decision = orch.run(_snapshot())
        timed_out = [row for row in decision.agent_results if "AGENT_TIMEOUT" in row.risk_flags]
        self.assertEqual(len(timed_out), 1)
        self.assertEqual(decision.proposed_action, CandidateAction.NONE)
        self.assertNotEqual(decision.decision, "PAPER_READY")

    def test_specialists_run_concurrently(self) -> None:
        barrier = threading.Barrier(2)
        started: list[str] = []
        orch = AgentCycleOrchestrator(
            specialists=(
                _FastAgent("a", started, barrier),
                _FastAgent("b", started, barrier),
            ),
            agent_timeout_seconds=2.0,
        )
        decision = orch.run(_snapshot())
        self.assertEqual(len(decision.agent_results), 2)
        self.assertEqual({row.status for row in decision.agent_results}, {AgentStatus.PASS})
        self.assertEqual(set(started), {"a", "b"})

    def test_deterministic_result_ordering(self) -> None:
        # Finish order is reverse of registration; output order must stay registration order.
        hold_late = threading.Event()
        hold_early = threading.Event()
        release_all = threading.Event()

        class _GateAgent:
            def __init__(self, name: str, hold: threading.Event) -> None:
                self.agent_name = name
                self.agent_version = f"{name}.v1"
                self._hold = hold

            def analyze(self, snapshot):
                self._hold.set()
                release_all.wait(timeout=2.0)
                return AgentResult(
                    agent_name=self.agent_name,
                    agent_version=self.agent_version,
                    snapshot_id=snapshot.snapshot_id,
                    decision_timestamp=snapshot.decision_timestamp,
                    status=AgentStatus.PASS,
                    observations=(self.agent_name,),
                    metrics_used=(),
                    candidate_action=CandidateAction.ABSTAIN,
                    candidate_instrument=None,
                    entry_reason=None,
                    invalidation_reason=None,
                    risk_flags=(),
                    missing_data=(),
                )

        first = _GateAgent("first", hold_late)
        second = _GateAgent("second", hold_early)
        orch = AgentCycleOrchestrator(specialists=(first, second), agent_timeout_seconds=2.0)

        def _run():
            return orch.run(_snapshot())

        # Ensure both are running, then release second-finisher first via shared event.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_run)
            self.assertTrue(hold_late.wait(timeout=2.0))
            self.assertTrue(hold_early.wait(timeout=2.0))
            release_all.set()
            decision = fut.result(timeout=2.0)
        self.assertEqual(
            [row.agent_name for row in decision.agent_results],
            ["first", "second"],
        )

    def test_stable_cycle_id_is_deterministic(self) -> None:
        snap = _snapshot()
        expected = stable_cycle_id(snap.snapshot_id, snap.decision_timestamp)
        orch = AgentCycleOrchestrator(specialists=(_OpenAgent(),))
        first = orch.run(snap)
        second = AgentCycleOrchestrator(specialists=(_OpenAgent(),)).run(snap)
        self.assertEqual(first.cycle_id, expected)
        self.assertEqual(second.cycle_id, expected)
        self.assertEqual(first.cycle_id, second.cycle_id)

    def test_second_pass_disabled_by_default(self) -> None:
        calls = {"n": 0}

        class _CountingAgent:
            agent_name = "counter"
            agent_version = "counter.v1"

            def analyze(self, snapshot):
                calls["n"] += 1
                return AgentResult(
                    agent_name=self.agent_name,
                    agent_version=self.agent_version,
                    snapshot_id=snapshot.snapshot_id,
                    decision_timestamp=snapshot.decision_timestamp,
                    status=AgentStatus.PASS,
                    observations=("count",),
                    metrics_used=(),
                    candidate_action=CandidateAction.ABSTAIN,
                    candidate_instrument=None,
                    entry_reason=None,
                    invalidation_reason=None,
                    risk_flags=(),
                    missing_data=(),
                )

        def always(results: tuple[AgentResult, ...], debate: DebateSummary) -> bool:
            return True

        default = AgentCycleOrchestrator(
            specialists=(_CountingAgent(),),
            second_pass_rule=always,
        )
        self.assertFalse(default.allow_second_pass)
        default.run(_snapshot())
        self.assertEqual(calls["n"], 1)

        calls["n"] = 0
        opted = AgentCycleOrchestrator(
            specialists=(_CountingAgent(),),
            allow_second_pass=True,
            second_pass_rule=always,
        )
        opted.run(_snapshot())
        self.assertEqual(calls["n"], 2)

    def test_risk_guard_still_blocks_paper_opens(self) -> None:
        orch = AgentCycleOrchestrator(specialists=(_OpenAgent(), _OpenAgent()))
        decision = orch.run(_snapshot())
        self.assertEqual(decision.decision, "REJECTED_BY_RISK")
        self.assertFalse(decision.risk.approved)
        self.assertEqual(decision.paper_execution_result, "BLOCKED_BY_RISK")


if __name__ == "__main__":
    unittest.main()
