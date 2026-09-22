"""Requirement 4B orchestration: aggregation, replay, timeouts, safety."""

from __future__ import annotations

import ast
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from grow.clock import IST
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.orchestration import AnalysisOrchestrator, aggregate_outputs, validate_agent_output
from grow.orchestration.cycle import AnalysisCycleStore
from grow.orchestration.dispatcher import dispatch_agents


def _pass_result(snapshot, *, agent_name: str, agent_version: str, cycle_id: str, **kwargs) -> AgentResult:
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
        findings=("OK",),
        data_quality_concerns=(),
        assumptions=(),
        evidence=("e",),
        metrics_used=(),
        candidate_action=CandidateAction.NONE,
        candidate_instrument=None,
        entry_reason=None,
        invalidation_reason=None,
        risk_flags=(),
        missing_data=(),
        cycle_id=cycle_id,
    )
    defaults.update(kwargs)
    return AgentResult(**defaults)


class _SlowAgent:
    agent_name = "slow"
    agent_version = "slow.v1"

    def __init__(self, delay: float = 0.5, name: str = "slow") -> None:
        self.delay = delay
        self.agent_name = name
        self.agent_version = f"{name}.v1"

    def analyze(self, snapshot, *, cycle_id: str = ""):
        time.sleep(self.delay)
        return _pass_result(
            snapshot,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            cycle_id=cycle_id,
            observations=("slow",),
            findings=("OK",),
            candidate_action=CandidateAction.PAPER_OPEN,
            candidate_instrument="NIFTY-25000-CE",
            entry_reason="should-not-survive-timeout",
        )


class _BoomAgent:
    agent_name = "boom"
    agent_version = "boom.v1"

    def analyze(self, snapshot, *, cycle_id: str = ""):
        raise RuntimeError("specialist exploded")


class _HealthyAgent:
    def __init__(self, name: str, delay: float = 0.0) -> None:
        self.agent_name = name
        self.agent_version = f"{name}.v1"
        self.delay = delay

    def analyze(self, snapshot, *, cycle_id: str = ""):
        if self.delay:
            time.sleep(self.delay)
        return _pass_result(
            snapshot,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            cycle_id=cycle_id,
            observations=(self.agent_name,),
            findings=("HEALTHY",),
        )


class _OrderAgent:
    def __init__(self, name: str, hold: threading.Event, release: threading.Event) -> None:
        self.agent_name = name
        self.agent_version = f"{name}.v1"
        self._hold = hold
        self._release = release

    def analyze(self, snapshot, *, cycle_id: str = ""):
        self._hold.set()
        self._release.wait(timeout=2.0)
        return _pass_result(
            snapshot,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            cycle_id=cycle_id,
            observations=(self.agent_name,),
        )


class _BullAgent:
    agent_name = "bull"
    agent_version = "bull.v1"

    def analyze(self, snapshot, *, cycle_id: str = ""):
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=("up",),
            calculated_metrics={},
            interpretation=("bullish structure",),
            findings=("TRENDING_UP_CANDIDATE",),
            data_quality_concerns=(),
            assumptions=(),
            evidence=("e",),
            metrics_used=(),
            candidate_action=CandidateAction.ABSTAIN,
            candidate_instrument=None,
            entry_reason=None,
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
            cycle_id=cycle_id,
        )


class _BearAgent:
    agent_name = "bear"
    agent_version = "bear.v1"

    def analyze(self, snapshot, *, cycle_id: str = ""):
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=("down",),
            calculated_metrics={},
            interpretation=("bearish structure",),
            findings=("TRENDING_DOWN_CANDIDATE",),
            data_quality_concerns=(),
            assumptions=(),
            evidence=("e",),
            metrics_used=(),
            candidate_action=CandidateAction.ABSTAIN,
            candidate_instrument=None,
            entry_reason=None,
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
            cycle_id=cycle_id,
        )


def _snap():
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
        diagnostics={"history_closes": [100.0 + i * 0.5 for i in range(20)]},
    )


class Orchestration4BTests(unittest.TestCase):
    def test_analysis_package_is_deterministic_for_fixed_inputs(self) -> None:
        snap = _snap()
        orch = AnalysisOrchestrator(configured_strategies=("trend", "momentum"))
        first = orch.run(snap, cycle_id="cycle-fixed")
        store = AnalysisCycleStore()
        orch2 = AnalysisOrchestrator(
            configured_strategies=("trend", "momentum"),
            store=store,
        )
        second = orch2.run(snap, cycle_id="cycle-fixed")
        self.assertEqual(first.package_digest, second.package_digest)
        self.assertEqual(first.cycle_summary, second.cycle_summary)
        self.assertEqual(first.snapshot_version, snap.version)
        self.assertFalse(first.to_dict()["broker_order_path"])

    def test_replay_uses_same_snapshot(self) -> None:
        snap = _snap()
        store = AnalysisCycleStore()
        orch = AnalysisOrchestrator(configured_strategies=("trend",), store=store)
        original = orch.run(snap, cycle_id="cycle-replay-src")
        replayed = orch.replay("cycle-replay-src")
        self.assertEqual(replayed.snapshot_id, original.snapshot_id)
        self.assertEqual(replayed.snapshot_version, original.snapshot_version)
        self.assertTrue(replayed.cycle_id.startswith("replay-"))
        # Same specialists + same snapshot → same digest body aside from cycle id stamping.
        self.assertEqual(len(replayed.agent_outputs), len(original.agent_outputs))

    def test_conflicts_preserved(self) -> None:
        orch = AnalysisOrchestrator(specialists=(_BullAgent(), _BearAgent()))
        package = orch.run(_snap(), cycle_id="cycle-conflict")
        self.assertTrue(any(c.startswith("DIRECTION_CONFLICT") for c in package.conflicts))
        self.assertTrue(package.conflicting_evidence or package.supporting_evidence)

    def test_timeout_marks_agent_unavailable(self) -> None:
        orch = AnalysisOrchestrator(
            specialists=(_SlowAgent(delay=0.5),),
            agent_timeout_seconds=0.05,
        )
        package = orch.run(_snap(), cycle_id="cycle-timeout")
        self.assertEqual(package.agent_outputs[0].status, AgentStatus.ERROR)
        self.assertIn("TIMEOUT", package.agent_outputs[0].invalidation_reason or "")
        self.assertIn("slow", package.unavailable_agents)

    def test_a_bounded_timeout_does_not_wait_for_slow_worker(self) -> None:
        """Orchestration must return after timeout without waiting for sleep to finish."""
        orch = AnalysisOrchestrator(
            specialists=(_SlowAgent(delay=0.5),),
            agent_timeout_seconds=0.05,
        )
        started = time.monotonic()
        package = orch.run(_snap(), cycle_id="cycle-timeout-bound")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.35, msg=f"dispatcher blocked too long: {elapsed:.3f}s")
        self.assertGreaterEqual(elapsed, 0.04)
        row = package.agent_outputs[0]
        self.assertEqual(row.status, AgentStatus.ERROR)
        self.assertIn("AGENT_TIMEOUT", row.risk_flags)

    def test_b_timeout_classification(self) -> None:
        orch = AnalysisOrchestrator(
            specialists=(_SlowAgent(delay=0.5),),
            agent_timeout_seconds=0.05,
        )
        package = orch.run(_snap(), cycle_id="cycle-timeout-class")
        row = package.agent_outputs[0]
        self.assertEqual(row.status, AgentStatus.ERROR)
        self.assertIn("AGENT_TIMEOUT", row.risk_flags)
        self.assertIn("TIMEOUT", row.invalidation_reason or "")
        self.assertEqual(row.findings, ("AGENT_UNAVAILABLE",))
        self.assertEqual(row.candidate_action, CandidateAction.NONE)
        self.assertIsNone(row.confidence)
        self.assertNotIn("AGENT_FAILURE", row.risk_flags)

    def test_c_ordinary_exception_classification(self) -> None:
        orch = AnalysisOrchestrator(
            specialists=(_BoomAgent(),),
            agent_timeout_seconds=1.0,
        )
        package = orch.run(_snap(), cycle_id="cycle-failure-class")
        row = package.agent_outputs[0]
        self.assertEqual(row.status, AgentStatus.ERROR)
        self.assertIn("AGENT_FAILURE", row.risk_flags)
        self.assertNotIn("AGENT_TIMEOUT", row.risk_flags)
        self.assertEqual(row.findings, ("AGENT_UNAVAILABLE",))
        self.assertEqual(row.candidate_action, CandidateAction.NONE)

    def test_d_parallel_execution_wall_clock(self) -> None:
        delay = 0.12
        specialists = (
            _HealthyAgent("a", delay=delay),
            _HealthyAgent("b", delay=delay),
            _HealthyAgent("c", delay=delay),
        )
        orch = AnalysisOrchestrator(specialists=specialists, agent_timeout_seconds=2.0)
        started = time.monotonic()
        package = orch.run(_snap(), cycle_id="cycle-parallel")
        elapsed = time.monotonic() - started
        self.assertEqual(len(package.agent_outputs), 3)
        self.assertEqual({row.status for row in package.agent_outputs}, {AgentStatus.PASS})
        # Parallel ≈ max(delay); sequential would be ~3*delay.
        self.assertLess(elapsed, delay * 2.5, msg=f"expected parallel wall clock, got {elapsed:.3f}s")
        self.assertGreaterEqual(elapsed, delay * 0.5)

    def test_e_deterministic_result_ordering(self) -> None:
        hold_first = threading.Event()
        hold_second = threading.Event()
        hold_third = threading.Event()
        release = threading.Event()
        specialists = (
            _OrderAgent("first", hold_first, release),
            _OrderAgent("second", hold_second, release),
            _OrderAgent("third", hold_third, release),
        )
        snap = _snap()

        def _run():
            return dispatch_agents(
                cycle_id="cycle-order",
                snapshot=snap,
                specialists=specialists,
                timeout_seconds=2.0,
            )

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_run)
            self.assertTrue(hold_first.wait(timeout=2.0))
            self.assertTrue(hold_second.wait(timeout=2.0))
            self.assertTrue(hold_third.wait(timeout=2.0))
            release.set()
            accepted, _rejected, _records = fut.result(timeout=2.0)
        self.assertEqual(
            [row.agent_name for row in accepted],
            ["first", "second", "third"],
        )

    def test_f_timeout_with_healthy_specialists(self) -> None:
        orch = AnalysisOrchestrator(
            specialists=(
                _HealthyAgent("alpha"),
                _SlowAgent(delay=0.5, name="slowpoke"),
                _HealthyAgent("beta"),
                _BullAgent(),
            ),
            agent_timeout_seconds=0.05,
        )
        package = orch.run(_snap(), cycle_id="cycle-mixed-timeout")
        by_name = {row.agent_name: row for row in package.agent_outputs}
        self.assertEqual(
            [row.agent_name for row in package.agent_outputs],
            ["alpha", "slowpoke", "beta", "bull"],
        )
        self.assertEqual(by_name["alpha"].status, AgentStatus.PASS)
        self.assertEqual(by_name["beta"].status, AgentStatus.PASS)
        self.assertEqual(by_name["bull"].status, AgentStatus.PASS)
        timed = by_name["slowpoke"]
        self.assertEqual(timed.status, AgentStatus.ERROR)
        self.assertIn("AGENT_TIMEOUT", timed.risk_flags)
        self.assertEqual(timed.candidate_action, CandidateAction.NONE)
        self.assertIn("slowpoke", package.unavailable_agents)
        # Timed-out agent must not create an actionable recommendation.
        self.assertNotEqual(timed.candidate_action, CandidateAction.PAPER_OPEN)
        # ERROR agents must not cast direction votes from fabricated findings.
        self.assertFalse(any("slowpoke" in c for c in package.conflicts if c.startswith("DIRECTION_")))

    def test_version_mismatch_rejected(self) -> None:
        snap = _snap()
        bad = AgentResult(
            agent_name="x",
            agent_version="v",
            snapshot_id=snap.snapshot_id,
            snapshot_version="not-the-version",
            decision_timestamp=snap.decision_timestamp,
            status=AgentStatus.PASS,
            observations=(),
            calculated_metrics={},
            interpretation=(),
            findings=(),
            data_quality_concerns=(),
            assumptions=(),
            evidence=(),
            metrics_used=(),
            candidate_action=CandidateAction.NONE,
            candidate_instrument=None,
            entry_reason=None,
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
            cycle_id="c1",
        )
        outcome = validate_agent_output(bad, snapshot=snap, cycle_id="c1")
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.reason, "SNAPSHOT_VERSION_MISMATCH")

    def test_aggregation_keeps_errors_visible(self) -> None:
        as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
        err = AgentResult(
            agent_name="err",
            agent_version="v",
            snapshot_id="s",
            snapshot_version="v",
            decision_timestamp=as_of,
            status=AgentStatus.ERROR,
            observations=("fail",),
            calculated_metrics={},
            interpretation=(),
            findings=("AGENT_UNAVAILABLE",),
            data_quality_concerns=(),
            assumptions=(),
            evidence=(),
            metrics_used=(),
            candidate_action=CandidateAction.NONE,
            candidate_instrument=None,
            entry_reason=None,
            invalidation_reason="boom",
            risk_flags=("AGENT_FAILURE",),
            missing_data=(),
            cycle_id="c",
        )
        package = aggregate_outputs(
            cycle_id="c",
            snapshot_id="s",
            snapshot_version="v",
            as_of=as_of,
            outputs=(err,),
            rejected_outputs=(),
            dispatch_records=(),
        )
        self.assertIn("err", package.unavailable_agents)
        self.assertIn("error=1", package.cycle_summary)

    def test_no_broker_imports_in_agents_or_orchestration(self) -> None:
        roots = [
            Path("grow/agents"),
            Path("grow/orchestration"),
            Path("grow/decision"),
            Path("grow/market_data"),
        ]
        forbidden = {"kiteconnect", "zerodha", "broker", "place_order", "orders.create"}
        for root in roots:
            for path in root.rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [node.module or ""]
                    else:
                        continue
                    joined = " ".join(names).lower()
                    for token in forbidden:
                        self.assertNotIn(token, joined, msg=f"{path}: {joined}")


if __name__ == "__main__":
    unittest.main()
