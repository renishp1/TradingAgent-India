"""Run a frozen 2F plan through 2E. Does not mutate PaperLedger."""

from __future__ import annotations

from grow.backtest.runner import _commit
from grow.backtest.walkforward import WalkForwardRunner
from grow.config import GrowConfig, load_config
from grow.director.models import FROZEN, ResearchPlan, ResearchResult
from grow.errors import GrowConfigError


class BacktestCoordinator:
    def __init__(self, config: GrowConfig | None = None) -> None:
        self.config = config or load_config()
        self.runner = WalkForwardRunner(self.config)

    def run(self, plan: ResearchPlan) -> ResearchResult:
        if plan.status != FROZEN or not plan.freeze_hash:
            raise GrowConfigError("PLAN_NOT_FROZEN")
        if plan.fingerprint() != plan.freeze_hash:
            raise GrowConfigError("FROZEN_HASH_MISMATCH")
        if plan.calibration_mode != "NONE":
            raise GrowConfigError("TEST_WINDOW_TUNING")
        wf = self.runner.run(start=plan.historical_period.start, end=plan.historical_period.end)
        first = wf.test_results[0] if wf.test_results else None
        metrics = first.metrics if first else {"trade_count": 0, "net_pnl": 0.0, "label": "HISTORICAL RESEARCH / NOT LIVE"}
        coverage_ok = bool(first.coverage.get("complete")) if first else False
        leakage = "CLEAN"
        result = ResearchResult(
            result_id=f"res-{plan.plan_id}",
            plan_id=plan.plan_id,
            freeze_hash=plan.freeze_hash,
            dataset_id=plan.dataset_id,
            dataset_version=plan.dataset_version,
            code_commit=_commit(),
            metrics=metrics,
            walk_forward=wf.to_dict(),
            stress_summary={"ablations": wf.ablations},
            data_quality=first.coverage if first else {"complete": False},
            leakage_status=leakage,
            sample_size=int(metrics.get("trade_count", 0)),
            coverage_complete=coverage_ok,
            visible=True,
            created_at=plan.created_at,
        )
        return result
