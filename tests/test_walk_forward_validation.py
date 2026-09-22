"""Requirement Walk-forward Validation — historical & out-of-sample evaluation."""

from __future__ import annotations

import ast
import inspect
import unittest
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from grow.backtest.calendar import weekday_sessions
from grow.backtest.costs import CostModel, SlippageModel
from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.errors import GrowConfigError
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.orchestration.models import AggregateAnalysisPackage
from grow.validation.artifacts import ArtifactStore, assert_reproducible, metrics_equal
from grow.validation.availability import contract_available_at, quote_on_historical_chain
from grow.validation.freeze import assert_final_eval_isolation, freeze_config, plan_from_config
from grow.validation.metrics import calculate_metrics
from grow.validation.pit import (
    assert_dataset_versions_match,
    assert_no_random_split,
    bar_is_available,
    filter_quotes_pit,
    quote_is_available,
    reject_future_feature,
    snapshot_leakage_flags,
)
from grow.validation.replay import HistoricalCycle, HistoricalPaperReplay
from grow.validation.robustness import build_robustness_report, summarize_windows
from grow.validation.runner import WalkForwardValidationRunner
from grow.validation.windows import split_walk_windows

from tests.helpers import TEST_RISK_SECRET


ROOT = Path(__file__).resolve().parents[1]
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
    )
    payload.update(overrides)
    return OptionQuoteView(**payload)


def _snapshot(as_of, quotes=None, quality=DataQualityStatus.OK):
    if quotes is None:
        quotes = (_quote(as_of),)
    return build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=as_of,
        spot=2500.0,
        option_contracts=tuple(quotes),
        quality=quality,
    )


def _metrics(**overrides):
    payload = {
        "strategy": "trend",
        "direction": "BULLISH",
        "underlying": "RELIANCE",
        "limit_price": 100.0,
        "stop_loss": 80.0,
        "quantity": 1,
    }
    payload.update(overrides)
    return payload


def _result(snapshot, *, cycle_id: str, instrument="RELIANCE-2500-CE", **kwargs):
    return AgentResult(
        agent_name="strategy_research",
        agent_version="strategy_research.v2",
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
        metrics_used=("limit_price", "stop_loss", "quantity"),
        candidate_action=CandidateAction.PAPER_OPEN,
        candidate_instrument=instrument,
        entry_reason="wf-test",
        invalidation_reason=None,
        risk_flags=(),
        missing_data=(),
        confidence=0.4,
        cycle_id=cycle_id,
    )


def _package(snapshot, output, *, cycle_id: str):
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
        cycle_summary="wf-test",
        package_digest=f"pkg-{cycle_id}",
    )


def _config(**risk):
    config = load_config()
    config = replace(
        config,
        live_data=replace(config.live_data, session_timeout_seconds=86400 * 30),
        backtest=replace(
            config.backtest,
            train_sessions=3,
            validate_sessions=1,
            test_sessions=1,
            step_sessions=1,
            embargo_sessions=1,
            calibrate_on_test=False,
        ),
    )
    if risk:
        config = replace(config, risk=replace(config.risk, **risk))
    return config


def _cycle_for(day: date, *, open_trade: bool, cycle_id: str, ltp: float = 100.0, bid: float = 99.0):
    as_of = datetime(day.year, day.month, day.day, 11, 0, tzinfo=IST)
    snap = _snapshot(as_of, quotes=(_quote(as_of, ltp=ltp, bid=bid, ask=bid + 2),))
    if not open_trade:
        return _no_trade_cycle(day, cycle_id=cycle_id)
    output = _result(snap, cycle_id=cycle_id)
    package = _package(snap, output, cycle_id=cycle_id)
    return HistoricalCycle(snapshot=snap, package=package, session_date=day, regime="TREND")


def _no_trade_cycle(day: date, *, cycle_id: str):
    as_of = datetime(day.year, day.month, day.day, 11, 0, tzinfo=IST)
    snap = _snapshot(as_of)
    output = AgentResult(
        agent_name="strategy_research",
        agent_version="strategy_research.v2",
        snapshot_id=snap.snapshot_id,
        snapshot_version=snap.version,
        decision_timestamp=snap.decision_timestamp,
        status=AgentStatus.PASS,
        observations=("spot=2500",),
        calculated_metrics=_metrics(),
        interpretation=(),
        findings=("NO_SETUP",),
        data_quality_concerns=(),
        assumptions=("buyer-only",),
        evidence=(f"snapshot_id={snap.snapshot_id}",),
        metrics_used=("limit_price", "stop_loss", "quantity"),
        candidate_action=CandidateAction.ABSTAIN,
        candidate_instrument=None,
        entry_reason="none",
        invalidation_reason=None,
        risk_flags=(),
        missing_data=(),
        confidence=0.1,
        cycle_id=cycle_id,
    )
    package = AggregateAnalysisPackage(
        cycle_id=cycle_id,
        snapshot_id=snap.snapshot_id,
        snapshot_version=snap.version,
        as_of=snap.decision_timestamp,
        agent_outputs=(output,),
        rejected_outputs=(),
        dispatch_records=(),
        conflicts=(),
        supporting_evidence=(),
        conflicting_evidence=(),
        unavailable_agents=(),
        debate=DebateSummary(
            agreement=True,
            actions=("ABSTAIN",),
            agreeing_agents=(output.agent_name,),
            dissenting_agents=(),
            conflicts=(),
            evidence=(),
            insufficient_agents=(),
            error_agents=(),
        ),
        cycle_summary="wf-no-trade",
        package_digest=f"pkg-{cycle_id}",
    )
    return HistoricalCycle(snapshot=snap, package=package, session_date=day, regime="RANGE")


def _fixture_sessions() -> tuple[date, ...]:
    return weekday_sessions(date(2026, 9, 1), date(2026, 9, 18))


def _fixture_cycles(sessions: tuple[date, ...]) -> tuple[HistoricalCycle, ...]:
    cycles: list[HistoricalCycle] = []
    for i, day in enumerate(sessions):
        # Open early in the span; later days mark/exit via TP or session flow.
        if i % 4 == 0:
            cycles.append(_cycle_for(day, open_trade=True, cycle_id=f"open-{i}", ltp=100.0, bid=99.0))
            # Afternoon mark that can close via take-profit path if configured.
            afternoon = datetime(day.year, day.month, day.day, 14, 0, tzinfo=IST)
            snap = _snapshot(afternoon, quotes=(_quote(afternoon, ltp=130.0, bid=129.0, ask=131.0),))
            cycles.append(
                HistoricalCycle(
                    snapshot=snap,
                    package=_package(snap, _result(snap, cycle_id=f"mark-{i}"), cycle_id=f"mark-{i}"),
                    session_date=day,
                    regime="TREND",
                )
            )
        else:
            cycles.append(_no_trade_cycle(day, cycle_id=f"idle-{i}"))
    return tuple(cycles)


@dataclass(frozen=True)
class _Listed:
    underlying: str
    expiry: date
    strike: float
    option_type: str
    first_seen_at: datetime
    last_seen_at: datetime
    lot_size: int | None


class WalkForwardValidationTests(unittest.TestCase):
    def test_walk_forward_window_generation(self) -> None:
        sessions = _fixture_sessions()
        windows = split_walk_windows(
            sessions,
            train=3,
            validate=1,
            test=1,
            step=1,
            embargo=1,
        )
        self.assertGreaterEqual(len(windows), 1)
        for window in windows:
            self.assertLess(window.train[1], window.validate[0])
            self.assertLess(window.validate[1], window.test[0])

    def test_chronological_window_rejects_shuffled_sessions(self) -> None:
        sessions = (date(2026, 9, 3), date(2026, 9, 1), date(2026, 9, 2))
        with self.assertRaises(GrowConfigError) as ctx:
            split_walk_windows(sessions, train=1, validate=1, test=1, step=1, embargo=0)
        self.assertIn("WALK_FORWARD_NON_CHRONOLOGICAL", str(ctx.exception))

    def test_random_split_forbidden(self) -> None:
        with self.assertRaises(GrowConfigError):
            assert_no_random_split("random")
        with self.assertRaises(GrowConfigError):
            assert_no_random_split("kfold")

    def test_future_data_leakage_rejected(self) -> None:
        decision = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
        future = decision + timedelta(minutes=5)
        self.assertFalse(bar_is_available(bar_end=future, decision_at=decision))
        self.assertFalse(quote_is_available(_quote(future), decision))
        with self.assertRaises(GrowConfigError):
            reject_future_feature("rsi", future, decision)
        snap = _snapshot(decision, quotes=(_quote(future),))
        flags = snapshot_leakage_flags(snap)
        self.assertIn("LOOKAHEAD_OPTION_QUOTE", flags)
        kept = filter_quotes_pit((_quote(decision), _quote(future)), decision)
        self.assertEqual(len(kept), 1)

    def test_historical_contract_availability(self) -> None:
        decision = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
        listed = _Listed(
            underlying="RELIANCE",
            expiry=EXPIRY,
            strike=2500.0,
            option_type="CE",
            first_seen_at=datetime(2026, 9, 20, 9, 15, tzinfo=IST),
            last_seen_at=datetime(2026, 9, 24, 15, 30, tzinfo=IST),
            lot_size=1,
        )
        ok = contract_available_at(listed, decision)
        self.assertTrue(ok.available)
        early = contract_available_at(
            replace(listed, first_seen_at=datetime(2026, 9, 23, 9, 15, tzinfo=IST)),
            decision,
        )
        self.assertFalse(early.available)
        self.assertEqual(early.reason, "CONTRACT_NOT_LISTED")
        quote = _quote(decision)
        mapped = {"RELIANCE-2500-CE": listed}
        self.assertTrue(quote_on_historical_chain(quote, decision_at=decision, listed=mapped).available)
        future_q = _quote(decision + timedelta(hours=1))
        self.assertEqual(
            quote_on_historical_chain(future_q, decision_at=decision, listed=mapped).reason,
            "FUTURE_QUOTE",
        )
        self.assertEqual(
            quote_on_historical_chain(quote, decision_at=decision, listed={}).reason,
            "UNKNOWN_HISTORICAL_CONTRACT",
        )

    def test_dataset_version_mismatch(self) -> None:
        with self.assertRaises(GrowConfigError) as ctx:
            assert_dataset_versions_match(
                {"dataset_id": "a", "dataset_version": "1", "dataset_fingerprint": "x"},
                {"dataset_id": "a", "dataset_version": "2", "dataset_fingerprint": "x"},
            )
        self.assertIn("DATASET_VERSION_MISMATCH", str(ctx.exception))

    def test_fee_slippage_models_are_versioned(self) -> None:
        costs = CostModel()
        slip = SlippageModel(10)
        self.assertTrue(costs.version)
        self.assertTrue(slip.version)
        config = _config()
        frozen = freeze_config(config)
        self.assertTrue(frozen.fee_model_version)
        self.assertTrue(frozen.slippage_model_version)
        buy, buy_slip = slip.buy(100.0)
        self.assertGreater(buy, 100.0)
        self.assertGreater(buy_slip, 0.0)

    def test_metric_calculation_includes_sample_sizes(self) -> None:
        metrics = calculate_metrics(
            trades=[
                {"net_pnl": 10.0, "gross_pnl": 12.0, "total_cost": 2.0},
                {"net_pnl": -4.0, "gross_pnl": -3.0, "total_cost": 1.0},
            ],
            decisions=[{"status": "FILLED"}, {"status": "NO_TRADE"}, {"status": "NO_TRADE"}],
            equity=[100.0, 110.0, 106.0],
            starting_cash=100.0,
            daily_loss_breaches=1,
            per_trade_risk_breaches=0,
            blocked_count=0,
            data_quality_failures=1,
        )
        self.assertEqual(metrics["sample_size"], 2)
        self.assertEqual(metrics["trade_count"], 2)
        self.assertEqual(metrics["win_count"], 1)
        self.assertEqual(metrics["loss_count"], 1)
        self.assertEqual(metrics["max_drawdown"], -4.0)
        self.assertEqual(metrics["daily_loss_limit_breaches"], 1)
        self.assertEqual(metrics["no_trade_count"], 2)
        self.assertFalse(metrics["profitability_claim"])
        self.assertEqual(metrics["evidence_strength"], "WEAK")

    def test_final_evaluation_isolation(self) -> None:
        config = _config()
        frozen = freeze_config(config)
        assert_final_eval_isolation(frozen, config)
        mutated = replace(config, backtest=replace(config.backtest, calibrate_on_test=True))
        with self.assertRaises(GrowConfigError):
            freeze_config(mutated)
        with self.assertRaises(GrowConfigError):
            assert_final_eval_isolation(frozen, config, allow_tune=True)
        tweaked = replace(config, paper=replace(config.paper, starting_cash=config.paper.starting_cash + 1))
        with self.assertRaises(GrowConfigError) as ctx:
            assert_final_eval_isolation(frozen, tweaked)
        self.assertIn("EVAL_CONFIG_MUTATED", str(ctx.exception))

    def test_plan_rejects_test_tuning(self) -> None:
        config = _config()
        plan = plan_from_config(
            config,
            dataset_id="ds",
            dataset_version="v1",
            dataset_fingerprint="fp",
            provider_name="fixture",
        )
        self.assertEqual(plan.calibration_mode, "NONE")
        self.assertEqual(plan.trainable_parameters, ())
        self.assertTrue(plan.freeze_hash)
        with self.assertRaises(GrowConfigError):
            replace(plan, calibration_mode="GRID", freeze_hash=None).frozen()

    def test_end_to_end_walk_forward_paper_replay(self) -> None:
        config = _config()
        sessions = _fixture_sessions()
        cycles = _fixture_cycles(sessions)
        runner = WalkForwardValidationRunner(
            config,
            risk_secret=TEST_RISK_SECRET,
            dataset_id="grow.validation.fixture.v1",
            dataset_version="grow.validation.fixture.v1",
            dataset_fingerprint="fixture",
            provider_name="fixture",
            artifact_store=ArtifactStore(),
        )
        with self.assertRaises(GrowConfigError):
            runner.run(sessions=sessions, cycles=cycles, calibrate_on_test=True)
        result = runner.run(sessions=sessions, cycles=cycles, include_fee_stress=True)
        self.assertGreaterEqual(len(result.windows), 1)
        self.assertEqual(len(result.test_results), len(result.windows))
        self.assertEqual(len(result.train_results), len(result.windows))
        self.assertEqual(len(result.validation_results), len(result.windows))
        self.assertIn(result.leakage_status, {"CLEAN", "UNKNOWN", "LEAKAGE"})
        self.assertFalse(result.to_dict()["live"])
        self.assertFalse(result.to_dict()["broker_order_path"])
        self.assertFalse(result.to_dict()["profitability_claim"])
        self.assertTrue(result.artifacts)
        for row in result.test_results:
            self.assertIsNotNone(row.run)
            self.assertEqual(row.run.window_role, "test")
            self.assertEqual(row.run.fee_model_version, result.frozen.fee_model_version)
            self.assertIn("sample_size", row.metrics)
        self.assertIn("windows", result.robustness)
        self.assertTrue(result.robustness["windows"]["retained_all_windows"])
        self.assertFalse(result.robustness["cherry_picked"])
        self.assertIn("degradation_validation_to_oos", result.robustness)
        self.assertTrue(result.robustness["fee_slippage_variations"])

    def test_deterministic_replay(self) -> None:
        config = _config()
        sessions = _fixture_sessions()
        cycles = _fixture_cycles(sessions)
        runner = WalkForwardValidationRunner(config, risk_secret=TEST_RISK_SECRET)
        runner.replay_deterministic(sessions=sessions, cycles=cycles)
        a = runner.run(sessions=sessions, cycles=cycles, include_fee_stress=False)
        b = runner.run(sessions=sessions, cycles=cycles, include_fee_stress=False)
        self.assertEqual(a.combined_test_net, b.combined_test_net)
        self.assertTrue(
            metrics_equal(
                {f"w{i}": r.metrics for i, r in enumerate(a.test_results)},
                {f"w{i}": r.metrics for i, r in enumerate(b.test_results)},
            )
            or all(
                metrics_equal(x.metrics, y.metrics)
                for x, y in zip(a.test_results, b.test_results, strict=True)
            )
        )

    def test_robustness_retains_all_windows(self) -> None:
        with self.assertRaises(GrowConfigError):
            summarize_windows([{"net_pnl": 1}], retain_all=False)
        report = build_robustness_report(
            test_metrics=[{"net_pnl": 1.0, "sample_size": 2}, {"net_pnl": -3.0, "sample_size": 1}],
            validation_metrics=[{"net_pnl": 5.0, "sample_size": 2}],
            test_trades=[
                {"net_pnl": 10.0, "exit_day": "2026-09-01"},
                {"net_pnl": -1.0, "exit_day": "2026-09-02"},
            ],
        )
        self.assertEqual(report["windows"]["window_count"], 2)
        self.assertEqual(report["windows"]["min_net_pnl"], -3.0)
        self.assertTrue(report["concentration"]["dependent_on_few_trades"])

    def test_leakage_cycle_is_flagged(self) -> None:
        config = _config()
        day = date(2026, 9, 1)
        as_of = datetime(2026, 9, 1, 11, 0, tzinfo=IST)
        future = as_of + timedelta(minutes=30)
        snap = _snapshot(as_of, quotes=(_quote(future),))
        cycle = HistoricalCycle(
            snapshot=snap,
            package=_package(snap, _result(snap, cycle_id="leak"), cycle_id="leak"),
            session_date=day,
        )
        replay = HistoricalPaperReplay(config, risk_secret=TEST_RISK_SECRET)
        out = replay.run([cycle], role="test", start=day, end=day)
        self.assertIn("LOOKAHEAD_OPTION_QUOTE", out.leakage_flags)

    def test_no_broker_order_api_in_validation(self) -> None:
        folder = ROOT / "grow" / "validation"
        joined = ""
        for path in folder.glob("*.py"):
            joined += path.read_text(encoding="utf-8")
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    text = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
                    self.assertNotIn("grow.execution.live", text)
        self.assertNotIn("place_order", joined)
        self.assertNotIn("kite.orders", joined.lower())
        src = inspect.getsource(WalkForwardValidationRunner)
        self.assertIn("TEST_WINDOW_TUNING", src)
        self.assertIn("assert_final_eval_isolation", src)


if __name__ == "__main__":
    unittest.main()
