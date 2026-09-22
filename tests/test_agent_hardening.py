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


def _result(snapshot, *, agent_name: str, agent_version: str, **kwargs) -> AgentResult:
    defaults = dict(
        agent_name=agent_name,
        agent_version=agent_version,
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        decision_timestamp=snapshot.decision_timestamp,
        status=AgentStatus.PASS,
        observations=("ok",),
        calculated_metrics={},
        interpretation=(),
        findings=(),
        data_quality_concerns=(),
        assumptions=(),
        evidence=("test",),
        metrics_used=(),
        candidate_action=CandidateAction.ABSTAIN,
        candidate_instrument=None,
        entry_reason=None,
        invalidation_reason=None,
        risk_flags=(),
        missing_data=(),
    )
    defaults.update(kwargs)
    return AgentResult(**defaults)


class _FastAgent:
    def __init__(self, name: str, started: list[str], barrier: threading.Barrier | None = None) -> None:
        self.agent_name = name
        self.agent_version = f"{name}.v1"
        self._started = started
        self._barrier = barrier

    def analyze(self, snapshot, *, cycle_id: str = ""):
        self._started.append(self.agent_name)
        if self._barrier is not None:
            self._barrier.wait(timeout=2.0)
        return _result(
            snapshot,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            cycle_id=cycle_id,
            entry_reason="fast",
        )


class _SlowAgent:
    agent_name = "slow"
    agent_version = "slow.v1"

    def __init__(self, delay: float = 5.0) -> None:
        self.delay = delay

    def analyze(self, snapshot, *, cycle_id: str = ""):
        time.sleep(self.delay)
        return _result(
            snapshot,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            cycle_id=cycle_id,
            observations=("too late",),
            candidate_action=CandidateAction.PAPER_OPEN,
            candidate_instrument="NIFTY-25000-CE",
            entry_reason="should-not-apply",
        )


class _OpenAgent:
    agent_name = "force_open"
    agent_version = "force_open.v1"

    def analyze(self, snapshot, *, cycle_id: str = ""):
        return _result(
            snapshot,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            cycle_id=cycle_id,
            observations=("force open",),
            findings=("FORCE_OPEN",),
            candidate_action=CandidateAction.PAPER_OPEN,
            candidate_instrument="NIFTY-25000-CE",
            entry_reason="test",
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
        hold_late = threading.Event()
        hold_early = threading.Event()
        release_all = threading.Event()

        class _GateAgent:
            def __init__(self, name: str, hold: threading.Event) -> None:
                self.agent_name = name
                self.agent_version = f"{name}.v1"
                self._hold = hold

            def analyze(self, snapshot, *, cycle_id: str = ""):
                self._hold.set()
                release_all.wait(timeout=2.0)
                return _result(
                    snapshot,
                    agent_name=self.agent_name,
                    agent_version=self.agent_version,
                    cycle_id=cycle_id,
                    observations=(self.agent_name,),
                )

        first = _GateAgent("first", hold_late)
        second = _GateAgent("second", hold_early)
        orch = AgentCycleOrchestrator(specialists=(first, second), agent_timeout_seconds=2.0)

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(orch.run, _snapshot())
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

            def analyze(self, snapshot, *, cycle_id: str = ""):
                calls["n"] += 1
                return _result(
                    snapshot,
                    agent_name=self.agent_name,
                    agent_version=self.agent_version,
                    cycle_id=cycle_id,
                    observations=("count",),
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
