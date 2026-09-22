"""Run a frozen 2F plan through 2E. Does not mutate PaperLedger."""

from __future__ import annotations

from grow.backtest.calendar import FIXTURE_CALENDAR, WeekdayFixtureCalendar
from grow.backtest.runner import BacktestRunner, _commit
from grow.config import GrowConfig, load_config
from grow.director.catalog import FIXTURE_DATASET
from grow.director.models import CONFIG_CANDIDATE, FROZEN, ResearchPlan, ResearchResult
from grow.errors import GrowConfigError

_LEAKAGE = frozenset({"CLEAN", "LEAKAGE", "UNKNOWN"})


class BacktestCoordinator:
    def __init__(self, config: GrowConfig | None = None) -> None:
        self.config = config or load_config()
        self.runner = BacktestRunner(self.config, calendar=WeekdayFixtureCalendar())

    def run(self, plan: ResearchPlan) -> ResearchResult:
        if plan.status != FROZEN or not plan.freeze_hash:
            raise GrowConfigError("PLAN_NOT_FROZEN")
        if plan.fingerprint() != plan.freeze_hash:
            raise GrowConfigError("FROZEN_HASH_MISMATCH")
        if plan.calibration_mode != "NONE":
            raise GrowConfigError("TEST_WINDOW_TUNING")
        if plan.dataset_id != FIXTURE_DATASET:
            raise GrowConfigError("COORDINATOR_FIXTURE_ONLY")
        if plan.session_calendar_version != FIXTURE_CALENDAR:
            raise GrowConfigError("CALENDAR_MISMATCH")
        if plan.selected_baseline_config != CONFIG_CANDIDATE:
            raise GrowConfigError(f"UNDECLARED_CONFIG:{plan.selected_baseline_config}")
        if any(c.config_id != CONFIG_CANDIDATE for c in plan.candidate_configurations):
            raise GrowConfigError("UNDECLARED_CONFIG")
        want_stress = "cost_x2" in plan.stress_scenarios or "slippage_x2" in plan.stress_scenarios
        executed = self.runner.run(
            start=plan.test_window.start,
            end=plan.test_window.end,
            underlyings=plan.universe,
            ablation="full",
            include_stress=want_stress,
        )
        leakage = executed.leakage_status
        if leakage not in _LEAKAGE:
            raise GrowConfigError(f"LEAKAGE_STATUS:{leakage}")
        plan_ref = {
            "plan_id": plan.plan_id,
            "freeze_hash": plan.freeze_hash,
            "train_window": plan.train_window.to_dict(),
            "validation_window": plan.validation_window.to_dict(),
            "test_window": plan.test_window.to_dict(),
            "embargo_gap": plan.embargo_gap,
            "selected_baseline_config": plan.selected_baseline_config,
            "candidate_configurations": [c.to_dict() for c in plan.candidate_configurations],
            "stress_scenarios": list(plan.stress_scenarios),
            "metrics": list(plan.metrics),
            "acceptance_rules": list(plan.acceptance_rules),
            "dataset_id": plan.dataset_id,
            "dataset_version": plan.dataset_version,
            "session_calendar_version": plan.session_calendar_version,
            "universe": list(plan.universe),
        }
        return ResearchResult(
            result_id=f"res-{plan.plan_id}",
            plan_id=plan.plan_id,
            freeze_hash=plan.freeze_hash,
            dataset_id=plan.dataset_id,
            dataset_version=plan.dataset_version,
            code_commit=_commit(),
            metrics=executed.metrics,
            walk_forward={
                **plan_ref,
                "test_sessions": executed.coverage.get("sessions"),
                "ablations": {},
            },
            stress_summary={"requested": list(plan.stress_scenarios), "rows": list(executed.stress)},
            data_quality=executed.coverage,
            leakage_status=leakage,
            sample_size=int(executed.metrics.get("trade_count", 0)),
            coverage_complete=bool(executed.coverage.get("complete")),
            visible=True,
            created_at=plan.created_at,
        )
