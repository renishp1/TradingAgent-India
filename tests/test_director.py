from __future__ import annotations

import inspect
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta

from grow.clock import IST
from grow.config import load_config
from grow.director.catalog import FIXTURE_DATASET, UNAPPROVED_STUB
from grow.director.coordinator import BacktestCoordinator
from grow.director.director import FixtureDirector, LogicalClock
from grow.director.models import FROZEN, HOLD, READY, REJECT, ResearchResult, WindowSpec
from grow.errors import GrowConfigError
from tests.helpers import make_runtime


def _director() -> FixtureDirector:
    return FixtureDirector()


def _valid_plan(director: FixtureDirector | None = None, **kwargs):
    d = director or _director()
    params = dict(
        question="Does 2B+2C+2D keep positive net expectancy for NIFTY BUY-CE under spread/slippage?",
        hypothesis="Expectancy stays positive across unseen walk-forward test windows without one outlier.",
        start=date(2026, 9, 1),
        end=date(2026, 9, 18),
        universe=("NIFTY",),
    )
    params.update(kwargs)
    return d, d.plan(**params)


class DirectorTests(unittest.TestCase):
    def test_plan_freeze_run_review_hold_on_fixture(self) -> None:
        d, plan = _valid_plan()
        self.assertEqual(plan.status, READY)
        self.assertEqual(plan.calibration_mode, "NONE")
        self.assertEqual(plan.selected_baseline_config, "grow.default.v1")
        frozen = d.freeze(plan)
        self.assertEqual(frozen.status, FROZEN)
        self.assertEqual(frozen.freeze_hash, plan.fingerprint())
        paper = make_runtime(load_config()).ledger
        before = list(paper.book.fills)
        result = BacktestCoordinator().run(frozen)
        self.assertEqual(result.plan_id, frozen.plan_id)
        self.assertEqual(result.freeze_hash, frozen.freeze_hash)
        self.assertTrue(result.visible)
        review = d.review(frozen, result)
        self.assertEqual(review.status, HOLD)
        self.assertNotEqual(review.status, "ACCEPT_FOR_PAPER")
        self.assertEqual(result.walk_forward["test_window"], frozen.test_window.to_dict())
        self.assertEqual(result.walk_forward["train_window"], frozen.train_window.to_dict())
        self.assertEqual(result.walk_forward["selected_baseline_config"], frozen.selected_baseline_config)
        self.assertEqual(result.walk_forward["dataset_id"], frozen.dataset_id)
        self.assertIn(result.leakage_status, {"CLEAN", "LEAKAGE", "UNKNOWN"})
        self.assertEqual(list(paper.book.fills), before)

    def test_determinism(self) -> None:
        _, a = _valid_plan()
        _, b = _valid_plan()
        self.assertEqual(a.plan_id, b.plan_id)
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_unapproved_dataset_rejected(self) -> None:
        d, plan = _valid_plan(dataset_id=UNAPPROVED_STUB)
        with self.assertRaises(GrowConfigError) as ctx:
            d.freeze(plan)
        self.assertIn("DATASET_NOT_APPROVED", str(ctx.exception))

    def test_unknown_dataset_rejected(self) -> None:
        d, plan = _valid_plan(dataset_id="invented.vendor")
        with self.assertRaises(GrowConfigError) as ctx:
            d.freeze(plan)
        self.assertIn("UNKNOWN_DATASET", str(ctx.exception))

    def test_bad_granularity(self) -> None:
        d, plan = _valid_plan(granularity="M1")
        with self.assertRaises(GrowConfigError) as ctx:
            d.freeze(plan)
        self.assertIn("UNSUPPORTED_GRANULARITY", str(ctx.exception))

    def test_insufficient_coverage_hold_path(self) -> None:
        d, plan = _valid_plan(start=date(2026, 9, 21), end=date(2026, 9, 22))
        self.assertEqual(plan.status, "DRAFT")
        with self.assertRaises(GrowConfigError):
            d.freeze(plan)

    def test_window_order(self) -> None:
        d, plan = _valid_plan()
        broken = replace(plan, validation_window=plan.train_window, status=READY)
        with self.assertRaises(GrowConfigError) as ctx:
            d.freeze(broken)
        self.assertIn("WINDOW_ORDER", str(ctx.exception))

    def test_frozen_plan_immutable_and_new_id_on_change(self) -> None:
        d, plan = _valid_plan()
        frozen = d.freeze(plan)
        with self.assertRaises(GrowConfigError):
            d.freeze(frozen)
        changed = d.mutate_frozen(frozen, research_question=frozen.research_question + " revised")
        self.assertNotEqual(changed.plan_id, frozen.plan_id)
        self.assertNotEqual(changed.status, FROZEN)

    def test_acceptance_rules_required(self) -> None:
        d, plan = _valid_plan()
        broken = replace(plan, acceptance_rules=())
        with self.assertRaises(GrowConfigError) as ctx:
            d.freeze(broken)
        self.assertIn("MISSING_ACCEPTANCE_RULES", str(ctx.exception))

    def test_safety_mutation_rejected(self) -> None:
        d, plan = _valid_plan(question="enable live_trading and broker")
        with self.assertRaises(GrowConfigError) as ctx:
            d.freeze(plan)
        self.assertIn("SAFETY_MUTATION", str(ctx.exception))

    def test_blind_until_frozen(self) -> None:
        d, plan = _valid_plan()
        ghost = ResearchResult(
            result_id="x",
            plan_id=plan.plan_id,
            freeze_hash="nope",
            dataset_id=FIXTURE_DATASET,
            dataset_version=FIXTURE_DATASET,
            code_commit="x",
            metrics={},
            walk_forward={},
            stress_summary={},
            data_quality={},
            leakage_status="CLEAN",
            sample_size=0,
            coverage_complete=True,
            visible=True,
            created_at=plan.created_at,
        )
        with self.assertRaises(GrowConfigError) as ctx:
            d.review(plan, ghost)
        self.assertIn("TEST_BLIND_UNTIL_FROZEN", str(ctx.exception))

    def test_coordinator_requires_freeze(self) -> None:
        _, plan = _valid_plan()
        with self.assertRaises(GrowConfigError) as ctx:
            BacktestCoordinator().run(plan)
        self.assertIn("PLAN_NOT_FROZEN", str(ctx.exception))

    def test_leakage_review_rejects(self) -> None:
        d, plan = _valid_plan()
        frozen = d.freeze(plan)
        leaked = ResearchResult(
            result_id="leak",
            plan_id=frozen.plan_id,
            freeze_hash=frozen.freeze_hash or "",
            dataset_id=frozen.dataset_id,
            dataset_version=frozen.dataset_version,
            code_commit="x",
            metrics={"trade_count": 3},
            walk_forward={},
            stress_summary={},
            data_quality={},
            leakage_status="LEAKAGE",
            sample_size=3,
            coverage_complete=True,
            visible=True,
            created_at=frozen.created_at,
        )
        review = d.review(frozen, leaked)
        self.assertEqual(review.status, REJECT)

    def test_next_plan_new_id(self) -> None:
        d, plan = _valid_plan()
        frozen = d.freeze(plan)
        result = BacktestCoordinator().run(frozen)
        review = d.review(frozen, result)
        child = d.next_plan(review)
        self.assertNotEqual(child.plan_id, frozen.plan_id)
        self.assertEqual(child.parent_plan_id, frozen.plan_id)

    def test_no_execution_surface(self) -> None:
        from grow.director import coordinator, director, validate

        src = inspect.getsource(coordinator) + inspect.getsource(director) + inspect.getsource(validate)
        for banned in ("execute_trade", "place_order", "submit_order", "mutate_ledger", "def buy", "def sell"):
            self.assertNotIn(banned, src)
        self.assertNotIn("import grow.paper", src)
        self.assertNotIn("import grow.risk", src)
        self.assertNotIn('leakage_status="CLEAN"', src)
        self.assertNotIn("leakage_status = \"CLEAN\"", inspect.getsource(coordinator))
        self.assertNotIn("leakage = \"CLEAN\"", inspect.getsource(coordinator))


def _cfg(**flags):
    cfg = load_config()
    return replace(cfg, research_director=replace(cfg.research_director, **flags))


class DirectorPermissionTests(unittest.TestCase):
    def test_enabled_false_rejects_plan_freeze_review(self) -> None:
        d = FixtureDirector(_cfg(enabled=False))
        with self.assertRaises(GrowConfigError) as ctx:
            d.plan(
                question="q",
                hypothesis="h that is testable for NIFTY",
                start=date(2026, 9, 1),
                end=date(2026, 9, 18),
            )
        self.assertIn("DIRECTOR_DISABLED", str(ctx.exception))
        other = FixtureDirector()
        _, plan = _valid_plan(other)
        frozen = other.freeze(plan)
        with self.assertRaises(GrowConfigError):
            d.freeze(frozen)
        ghost = ResearchResult(
            result_id="x",
            plan_id=frozen.plan_id,
            freeze_hash=frozen.freeze_hash or "",
            dataset_id=FIXTURE_DATASET,
            dataset_version=FIXTURE_DATASET,
            code_commit="x",
            metrics={},
            walk_forward={},
            stress_summary={},
            data_quality={},
            leakage_status="CLEAN",
            sample_size=0,
            coverage_complete=True,
            visible=True,
            created_at=frozen.created_at,
        )
        with self.assertRaises(GrowConfigError) as ctx:
            d.review(frozen, ghost)
        self.assertIn("DIRECTOR_DISABLED", str(ctx.exception))

    def test_allow_plan_creation_false(self) -> None:
        d = FixtureDirector(_cfg(allow_plan_creation=False))
        with self.assertRaises(GrowConfigError) as ctx:
            d.plan(
                question="Does 2B+2C+2D keep positive net expectancy for NIFTY BUY-CE under spread/slippage?",
                hypothesis="Expectancy stays positive across unseen walk-forward test windows without one outlier.",
                start=date(2026, 9, 1),
                end=date(2026, 9, 18),
            )
        self.assertIn("PLAN_CREATION_DISABLED", str(ctx.exception))

    def test_allow_plan_freeze_false(self) -> None:
        _, plan = _valid_plan()
        d = FixtureDirector(_cfg(allow_plan_freeze=False))
        with self.assertRaises(GrowConfigError) as ctx:
            d.freeze(plan)
        self.assertIn("PLAN_FREEZE_DISABLED", str(ctx.exception))

    def test_allow_result_review_false(self) -> None:
        other = FixtureDirector()
        _, plan = _valid_plan(other)
        frozen = other.freeze(plan)
        d = FixtureDirector(_cfg(allow_result_review=False))
        ghost = ResearchResult(
            result_id="x",
            plan_id=frozen.plan_id,
            freeze_hash=frozen.freeze_hash or "",
            dataset_id=FIXTURE_DATASET,
            dataset_version=FIXTURE_DATASET,
            code_commit="x",
            metrics={},
            walk_forward={},
            stress_summary={},
            data_quality={},
            leakage_status="CLEAN",
            sample_size=0,
            coverage_complete=True,
            visible=True,
            created_at=frozen.created_at,
        )
        with self.assertRaises(GrowConfigError) as ctx:
            d.review(frozen, ghost)
        self.assertIn("RESULT_REVIEW_DISABLED", str(ctx.exception))


class DirectorCoordinatorTests(unittest.TestCase):
    def test_coordinator_uses_frozen_test_window_not_global(self) -> None:
        d, plan = _valid_plan()
        frozen = d.freeze(plan)
        first = BacktestCoordinator().run(frozen)
        changed = d.mutate_frozen(
            frozen,
            test_window=WindowSpec(date(2026, 9, 15), date(2026, 9, 16)),
        )
        self.assertNotEqual(changed.plan_id, frozen.plan_id)
        second_frozen = d.freeze(changed)
        second = BacktestCoordinator().run(second_frozen)
        self.assertEqual(second.walk_forward["test_window"], second_frozen.test_window.to_dict())
        self.assertNotEqual(first.walk_forward["test_window"], second.walk_forward["test_window"])
        self.assertEqual(second.walk_forward["freeze_hash"], second_frozen.freeze_hash)
        self.assertEqual(second.plan_id, second_frozen.plan_id)

    def test_unknown_leakage_is_not_clean(self) -> None:
        d, plan = _valid_plan()
        frozen = d.freeze(plan)
        unknown = ResearchResult(
            result_id="u",
            plan_id=frozen.plan_id,
            freeze_hash=frozen.freeze_hash or "",
            dataset_id=frozen.dataset_id,
            dataset_version=frozen.dataset_version,
            code_commit="x",
            metrics={},
            walk_forward={},
            stress_summary={},
            data_quality={},
            leakage_status="UNKNOWN",
            sample_size=0,
            coverage_complete=True,
            visible=True,
            created_at=frozen.created_at,
        )
        review = d.review(frozen, unknown)
        self.assertEqual(review.status, REJECT)
        self.assertIn("UNKNOWN_LEAKAGE", review.notable_failures)

    def test_clean_leakage_still_hold_on_fixture(self) -> None:
        d, plan = _valid_plan()
        frozen = d.freeze(plan)
        clean = ResearchResult(
            result_id="c",
            plan_id=frozen.plan_id,
            freeze_hash=frozen.freeze_hash or "",
            dataset_id=frozen.dataset_id,
            dataset_version=frozen.dataset_version,
            code_commit="x",
            metrics={"trade_count": 1},
            walk_forward={},
            stress_summary={},
            data_quality={},
            leakage_status="CLEAN",
            sample_size=1,
            coverage_complete=True,
            visible=True,
            created_at=frozen.created_at,
        )
        review = d.review(frozen, clean)
        self.assertEqual(review.status, HOLD)

    def test_audit_timestamps_use_injected_clock(self) -> None:
        clock = LogicalClock(datetime(2026, 2, 1, 10, 0, tzinfo=IST))
        d = FixtureDirector(clock=clock)
        _, plan = _valid_plan(d)
        self.assertEqual(plan.created_at, "2026-02-01T10:00:00+05:30")
        self.assertNotEqual(plan.created_at, plan.historical_period.start.isoformat())
        clock.set(datetime(2026, 2, 1, 11, 0, tzinfo=IST))
        frozen = d.freeze(plan)
        self.assertEqual(frozen.created_at, plan.created_at)
        self.assertEqual(frozen.test_freeze_at, "2026-02-01T11:00:00+05:30")
        self.assertNotEqual(frozen.test_freeze_at, frozen.created_at)
        self.assertNotEqual(frozen.test_freeze_at, frozen.historical_period.start.isoformat())


if __name__ == "__main__":
    unittest.main()

