"""Chronological walk-forward. Frozen config. Test windows are never tuned."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from grow.backtest.calendar import weekday_sessions
from grow.backtest.runner import BacktestResult, BacktestRunner
from grow.config import GrowConfig, load_config
from grow.errors import GrowConfigError


@dataclass(frozen=True)
class WalkWindow:
    train: tuple[date, date]
    validate: tuple[date, date]
    test: tuple[date, date]


@dataclass
class WalkForwardResult:
    windows: tuple[WalkWindow, ...]
    test_results: tuple[BacktestResult, ...]
    combined_test_net: float
    ablations: dict[str, float]
    plan: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "windows": [
                {
                    "train": [w.train[0].isoformat(), w.train[1].isoformat()],
                    "validate": [w.validate[0].isoformat(), w.validate[1].isoformat()],
                    "test": [w.test[0].isoformat(), w.test[1].isoformat()],
                }
                for w in self.windows
            ],
            "test_net_pnl": [item.metrics["net_pnl"] for item in self.test_results],
            "combined_test_net": self.combined_test_net,
            "ablations": self.ablations,
            "plan": self.plan,
            "label": "HISTORICAL RESEARCH / NOT LIVE",
        }


def split_windows(
    sessions: tuple[date, ...],
    *,
    train: int,
    validate: int,
    test: int,
    step: int,
    embargo: int,
) -> tuple[WalkWindow, ...]:
    windows: list[WalkWindow] = []
    i = 0
    block = train + embargo + validate + embargo + test
    while i + block <= len(sessions):
        t0 = i
        t1 = i + train
        v0 = t1 + embargo
        v1 = v0 + validate
        s0 = v1 + embargo
        s1 = s0 + test
        windows.append(
            WalkWindow(
                train=(sessions[t0], sessions[t1 - 1]),
                validate=(sessions[v0], sessions[v1 - 1]),
                test=(sessions[s0], sessions[s1 - 1]),
            )
        )
        i += step
    return tuple(windows)


class WalkForwardRunner:
    def __init__(self, config: GrowConfig | None = None) -> None:
        self.config = config or load_config()
        self.runner = BacktestRunner(self.config)

    def run(self, *, start: date, end: date, calibrate_on_test: bool | None = None) -> WalkForwardResult:
        bt = self.config.backtest
        if (calibrate_on_test if calibrate_on_test is not None else bt.calibrate_on_test):
            raise GrowConfigError("TEST_WINDOW_TUNING")
        sessions = weekday_sessions(start, end)
        windows = split_windows(
            sessions,
            train=bt.train_sessions,
            validate=bt.validate_sessions,
            test=bt.test_sessions,
            step=bt.step_sessions,
            embargo=bt.embargo_sessions,
        )
        tests: list[BacktestResult] = []
        for window in windows:
            tests.append(self.runner.run(start=window.test[0], end=window.test[1], ablation="full"))
        combined = round(sum(item.metrics["net_pnl"] for item in tests), 4)
        ablations: dict[str, float] = {}
        if tests:
            span_start, span_end = windows[0].test[0], windows[-1].test[1]
            for name in ("always_no_trade", "skip_ceo", "full"):
                ablations[name] = self.runner.run(start=span_start, end=span_end, ablation=name).metrics["net_pnl"]
        return WalkForwardResult(
            windows=windows,
            test_results=tuple(tests),
            combined_test_net=combined,
            ablations=ablations,
            plan={
                "train_sessions": bt.train_sessions,
                "validate_sessions": bt.validate_sessions,
                "test_sessions": bt.test_sessions,
                "step_sessions": bt.step_sessions,
                "embargo_sessions": bt.embargo_sessions,
                "calibrate_on_test": False,
                "trainable_parameters": (),
            },
        )
