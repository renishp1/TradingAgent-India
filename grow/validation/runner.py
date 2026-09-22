"""Frozen walk-forward validation runner.

Separates train / validation / out-of-sample test windows. Final evaluation
cannot tune or silently mutate the frozen configuration. No live broker path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

from grow.config import GrowConfig, load_config
from grow.errors import GrowConfigError
from grow.validation.artifacts import ArtifactStore, assert_reproducible
from grow.validation.freeze import (
    FrozenEvaluationConfig,
    assert_final_eval_isolation,
    freeze_config,
    plan_from_config,
    with_cost_stress,
)
from grow.validation.models import EvaluationRun, WalkForwardPlan, WindowRoleResult, make_run_id
from grow.validation.pit import assert_dataset_versions_match, assert_no_random_split
from grow.validation.replay import HistoricalCycle, HistoricalPaperReplay
from grow.validation.robustness import build_robustness_report, fee_slippage_matrix
from grow.validation.windows import WalkWindow, split_walk_windows


@dataclass
class WalkForwardValidationResult:
    plan: WalkForwardPlan
    frozen: FrozenEvaluationConfig
    windows: tuple[WalkWindow, ...]
    train_results: tuple[WindowRoleResult, ...]
    validation_results: tuple[WindowRoleResult, ...]
    test_results: tuple[WindowRoleResult, ...]
    robustness: dict[str, Any]
    artifacts: dict[str, str]
    combined_test_net: float
    leakage_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "frozen_fingerprint": self.frozen.fingerprint,
            "windows": [w.to_dict() for w in self.windows],
            "train_results": [r.to_dict() for r in self.train_results],
            "validation_results": [r.to_dict() for r in self.validation_results],
            "test_results": [r.to_dict() for r in self.test_results],
            "robustness": self.robustness,
            "artifacts": dict(self.artifacts),
            "combined_test_net": self.combined_test_net,
            "leakage_status": self.leakage_status,
            "live": False,
            "broker_order_path": False,
            "label": "WALK-FORWARD VALIDATION / NOT LIVE",
            "profitability_claim": False,
        }


class WalkForwardValidationRunner:
    """Rolling walk-forward validation over historical paper-replay cycles."""

    def __init__(
        self,
        config: GrowConfig | None = None,
        *,
        risk_secret: str,
        dataset_id: str = "grow.validation.fixture.v1",
        dataset_version: str = "grow.validation.fixture.v1",
        dataset_fingerprint: str = "fixture",
        provider_name: str = "fixture",
        artifact_store: ArtifactStore | None = None,
        listed_contracts: dict[str, Any] | None = None,
    ) -> None:
        self.config = config or load_config()
        self.config.assert_safe()
        if self.config.execution.live_trading_enabled or self.config.live_data.live_trading:
            raise GrowConfigError("LIVE_TRADING_FORBIDDEN")
        self.risk_secret = risk_secret
        self.dataset_id = dataset_id
        self.dataset_version = dataset_version
        self.dataset_fingerprint = dataset_fingerprint
        self.provider_name = provider_name
        self.artifacts = artifact_store or ArtifactStore()
        self.listed_contracts = listed_contracts
        self.frozen = freeze_config(self.config)
        self.plan = plan_from_config(
            self.config,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            dataset_fingerprint=dataset_fingerprint,
            provider_name=provider_name,
        )

    def run(
        self,
        *,
        sessions: tuple[date, ...],
        cycles: Sequence[HistoricalCycle],
        calibrate_on_test: bool | None = None,
        split_method: str = "chronological",
        include_fee_stress: bool = True,
    ) -> WalkForwardValidationResult:
        assert_no_random_split(split_method)
        if split_method != "chronological":
            raise GrowConfigError("NON_CHRONOLOGICAL_SPLIT")
        if calibrate_on_test if calibrate_on_test is not None else self.config.backtest.calibrate_on_test:
            raise GrowConfigError("TEST_WINDOW_TUNING")
        assert_final_eval_isolation(self.frozen, self.config, allow_tune=False)
        assert_dataset_versions_match(
            {
                "dataset_id": self.dataset_id,
                "dataset_version": self.dataset_version,
                "dataset_fingerprint": self.dataset_fingerprint,
            },
            {
                "dataset_id": self.plan.dataset_id,
                "dataset_version": self.plan.dataset_version,
                "dataset_fingerprint": self.plan.dataset_fingerprint,
            },
        )

        bt = self.config.backtest
        windows = split_walk_windows(
            sessions,
            train=bt.train_sessions,
            validate=bt.validate_sessions,
            test=bt.test_sessions,
            step=bt.step_sessions,
            embargo=bt.embargo_sessions,
        )
        if not windows:
            raise GrowConfigError("WALK_FORWARD_NO_WINDOWS")

        train_rows: list[WindowRoleResult] = []
        val_rows: list[WindowRoleResult] = []
        test_rows: list[WindowRoleResult] = []
        all_test_trades: list[dict[str, Any]] = []
        leakage_flags: list[str] = []

        replay = HistoricalPaperReplay(
            self.config,
            risk_secret=self.risk_secret,
            listed_contracts=self.listed_contracts,
        )

        for window in windows:
            # Train / validate are recorded for structure and robustness; they do
            # not select trainable parameters (calibration_mode=NONE).
            train = replay.run(cycles, role="train", start=window.train[0], end=window.train[1])
            validate = replay.run(cycles, role="validate", start=window.validate[0], end=window.validate[1])
            # Re-assert freeze immediately before OOS evaluation.
            assert_final_eval_isolation(self.frozen, self.config, allow_tune=False)
            test = replay.run(cycles, role="test", start=window.test[0], end=window.test[1])

            leakage_flags.extend(train.leakage_flags)
            leakage_flags.extend(validate.leakage_flags)
            leakage_flags.extend(test.leakage_flags)
            all_test_trades.extend(test.trades)

            train_rows.append(self._role_result("train", window, train))
            val_rows.append(self._role_result("validate", window, validate))
            test_rows.append(self._role_result("test", window, test))

        fee_stress: list[dict[str, Any]] = []
        if include_fee_stress and test_rows:
            span_start, span_end = windows[0].test[0], windows[-1].test[1]
            base_net = sum(float(r.metrics.get("net_pnl", 0)) for r in test_rows)
            costly = HistoricalPaperReplay(
                self.config,
                risk_secret=self.risk_secret,
                cost_stress=2.0,
                listed_contracts=self.listed_contracts,
            ).run(cycles, role="test", start=span_start, end=span_end)
            slippery_cfg = with_cost_stress(self.config, slip_mult=2.0)
            slippery = HistoricalPaperReplay(
                slippery_cfg,
                risk_secret=self.risk_secret,
                listed_contracts=self.listed_contracts,
            ).run(cycles, role="test", start=span_start, end=span_end)
            fee_stress = fee_slippage_matrix(
                base_net,
                cost_x2_net=float(costly.metrics.get("net_pnl", 0)),
                slip_x2_net=float(slippery.metrics.get("net_pnl", 0)),
            )

        robustness = build_robustness_report(
            test_metrics=[r.metrics for r in test_rows],
            validation_metrics=[r.metrics for r in val_rows],
            test_trades=all_test_trades,
            fee_stress=fee_stress,
        )
        combined = round(sum(float(r.metrics.get("net_pnl", 0)) for r in test_rows), 4)
        if any(flag.startswith("LOOKAHEAD") or flag == "FUTURE_QUOTE" for flag in leakage_flags):
            leakage_status = "LEAKAGE"
        elif not any(r.cycle_count for r in test_rows):
            leakage_status = "UNKNOWN"
        else:
            leakage_status = "CLEAN"

        summary = {
            "plan": self.plan.to_dict(),
            "windows": [w.to_dict() for w in windows],
            "test_metrics": [r.metrics for r in test_rows],
            "robustness": robustness,
            "combined_test_net": combined,
            "leakage_status": leakage_status,
        }
        self.artifacts.put("evaluation_summary", summary)
        self.artifacts.put("raw_test_trades", {"trades": all_test_trades})
        refs = self.artifacts.refs()

        # Final isolation: configuration fingerprint unchanged after the run.
        assert_final_eval_isolation(self.frozen, self.config, allow_tune=False)

        return WalkForwardValidationResult(
            plan=self.plan,
            frozen=self.frozen,
            windows=windows,
            train_results=tuple(train_rows),
            validation_results=tuple(val_rows),
            test_results=tuple(test_rows),
            robustness=robustness,
            artifacts=refs,
            combined_test_net=combined,
            leakage_status=leakage_status,
        )

    def replay_deterministic(
        self,
        *,
        sessions: tuple[date, ...],
        cycles: Sequence[HistoricalCycle],
    ) -> None:
        first = self.run(sessions=sessions, cycles=cycles, include_fee_stress=False)
        second = self.run(sessions=sessions, cycles=cycles, include_fee_stress=False)
        assert_reproducible(
            {"combined": first.combined_test_net, **{f"w{i}": r.metrics["net_pnl"] for i, r in enumerate(first.test_results)}},
            {"combined": second.combined_test_net, **{f"w{i}": r.metrics["net_pnl"] for i, r in enumerate(second.test_results)}},
        )

    def _role_result(self, role: str, window: WalkWindow, replay) -> WindowRoleResult:
        span = {"train": window.train, "validate": window.validate, "test": window.test}[role]
        run = EvaluationRun(
            run_id=make_run_id(
                {
                    "role": role,
                    "window": window.index,
                    "plan": self.plan.freeze_hash,
                    "start": span[0].isoformat(),
                    "end": span[1].isoformat(),
                }
            ),
            configuration_version=self.frozen.configuration_version,
            code_commit=self.frozen.code_commit,
            dataset_id=self.dataset_id,
            dataset_version=self.dataset_version,
            dataset_fingerprint=self.dataset_fingerprint,
            provider_name=self.provider_name,
            start=span[0],
            end=span[1],
            train=window.train,
            validate=window.validate,
            test=window.test,
            strategy_agent_config={
                "fill_model": self.frozen.fill_model,
                "entry_price_source": self.frozen.entry_price_source,
                "exit_price_source": self.frozen.exit_price_source,
                "slippage_bps": self.frozen.slippage_bps,
            },
            universe=self.frozen.universe,
            fee_model_version=self.frozen.fee_model_version,
            slippage_model_version=self.frozen.slippage_model_version,
            risk_limit_config=dict(self.frozen.risk_limits),
            cycle_count=replay.cycle_count,
            candidate_count=replay.candidate_count,
            trade_count=int(replay.metrics.get("trade_count", 0)),
            rejection_count=len(replay.rejections),
            metrics=replay.metrics,
            artifact_refs=self.artifacts.refs(),
            window_role=role,
            window_index=window.index,
            leakage_status="LEAKAGE" if replay.leakage_flags else "CLEAN",
            created_at=span[0].isoformat(),
            plan_freeze_hash=self.plan.freeze_hash,
        )
        self.artifacts.put(f"{role}-window-{window.index}", run.to_dict())
        return WindowRoleResult(
            role=role,
            window=window,
            metrics=replay.metrics,
            trades=list(replay.trades),
            decisions=list(replay.decisions),
            rejections=list(replay.rejections),
            leakage_flags=list(replay.leakage_flags),
            cycle_count=replay.cycle_count,
            candidate_count=replay.candidate_count,
            run=run,
        )
