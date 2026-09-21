"""BacktestRunner — fixture historical replay. No broker. No PaperLedger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from grow.backtest.calendar import CALENDAR_VERSION, DECISION_TIME, SessionCalendar, WeekdayFixtureCalendar, at_session
from grow.backtest.costs import CostModel, SlippageModel
from grow.backtest.ledger import BacktestLedger
from grow.backtest.metrics import calculate, stress_note
from grow.backtest.models import EXEC_MODEL, BacktestRunManifest, fingerprint_for
from grow.backtest.pipeline import DecisionPipeline
from grow.backtest.simulate import ExecutionSimulator
from grow.config import GrowConfig, load_config
from grow.data.factory import open_data_hub
from grow.errors import GrowConfigError
from grow.options.engine import IndexOptionsEngine
from grow.options.fixture import open_option_source
from grow.research.models import DECISION_SCHEMA, PACKET_SCHEMA
from grow.research.orchestrator import ResearchOrchestrator
from grow.strategies.engine import StrategyEngine

DATASET = "grow.data.fixture.v1"


def _commit() -> str:
    try:
        from pathlib import Path

        head = Path("/workspace/TradingAgent-India/.git/HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = Path("/workspace/TradingAgent-India/.git") / head.split(" ", 1)[1]
            return ref.read_text(encoding="utf-8").strip()[:40]
        return head[:40]
    except OSError:
        return "workspace"


def build_manifest(
    config: GrowConfig,
    *,
    start: date,
    end: date,
    ablation: str,
    calendar_version: str | None = None,
) -> BacktestRunManifest:
    bt = config.backtest
    required = {
        "dataset_id": DATASET,
        "dataset_version": DATASET,
        "calendar_version": calendar_version or CALENDAR_VERSION,
        "code_commit": _commit() or "workspace",
        "config_version": config.version,
        "strategy_version": "strategies.engine.v1",
        "options_engine_version": config.options.selection_version,
        "research_schema_version": f"{PACKET_SCHEMA}+{DECISION_SCHEMA}",
        "model_provider_mode": config.ai.provider,
        "cost_model_version": bt.cost_model_version,
        "slippage_model_version": bt.slippage_model_version,
        "execution_version": EXEC_MODEL,
        "evaluation_start": start.isoformat(),
        "evaluation_end": end.isoformat(),
        "timezone": "Asia/Kolkata",
        "strictness_mode": "strict" if bt.strict else "relaxed",
        "fill_model": bt.fill_model,
        "ablation": ablation,
    }
    missing = [key for key, value in required.items() if value in {None, ""}]
    if missing:
        raise GrowConfigError(f"incomplete backtest manifest: {missing}")
    fp = fingerprint_for(required)
    run_id = fp[:16]
    return BacktestRunManifest(
        run_id=run_id,
        prompt_versions=config.ai.prompt_versions,
        random_seed=None,
        created_at=start.isoformat(),
        fingerprint=fp,
        **required,
    )


@dataclass
class BacktestResult:
    manifest: BacktestRunManifest
    ledger: BacktestLedger
    metrics: dict[str, Any]
    stress: tuple[dict, ...]
    coverage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "metrics": self.metrics,
            "stress": list(self.stress),
            "coverage": self.coverage,
            "trades": [t.to_dict() for t in self.ledger.primary_trades],
            "decisions": [d.to_dict() for d in self.ledger.decisions],
            "equity": list(self.ledger.equity),
            "label": "HISTORICAL RESEARCH / NOT LIVE",
        }


class BacktestRunner:
    def __init__(self, config: GrowConfig | None = None, *, calendar: SessionCalendar | None = None) -> None:
        self.config = config or load_config()
        bt = self.config.backtest
        if bt.provider != "fixture":
            raise GrowConfigError("2E backtest.provider must be fixture.")
        if bt.fill_model != "ask_plus_slippage":
            raise GrowConfigError("2E fill_model is ask_plus_slippage.")
        if bt.max_open_positions != 1:
            raise GrowConfigError("2E v1 is one open position.")
        if bt.calibrate_on_test:
            raise GrowConfigError("TEST_WINDOW_TUNING")
        if calendar is None:
            if bt.provider != "fixture":
                raise GrowConfigError("historical runs require ExplicitSessionCalendar")
            calendar = WeekdayFixtureCalendar()
        self.calendar = calendar

    def run(
        self,
        *,
        start: date,
        end: date,
        underlyings: tuple[str, ...] | None = None,
        ablation: str = "full",
        cost_stress: float = 1.0,
        slip_stress: float = 1.0,
        include_stress: bool = False,
    ) -> BacktestResult:
        cfg = self.config
        bt = cfg.backtest
        names = underlyings or cfg.strategies.universe
        sessions = self.calendar.sessions(start, end)
        manifest = build_manifest(
            cfg, start=start, end=end, ablation=ablation, calendar_version=self.calendar.version
        )
        ledger = BacktestLedger(bt.starting_cash)
        hub = open_data_hub(cfg)
        pipeline = DecisionPipeline(
            hub=hub,
            strategies=StrategyEngine(cfg),
            options=IndexOptionsEngine(cfg),
            chains=open_option_source(),
            research=ResearchOrchestrator(cfg),
            simulator=ExecutionSimulator(
                SlippageModel(bt.slippage_bps, stress=slip_stress),
                quantity=bt.quantity,
                strict=bt.strict,
            ),
            costs=CostModel(stress=cost_stress),
            ledger=ledger,
            run_id=manifest.run_id,
            ablation=ablation,
            config_version=cfg.version,
            lot_size=bt.lot_size,
        )
        expected = 0
        for day in sessions:
            as_of = at_session(day, DECISION_TIME)
            for ticker in names:
                expected += 1
                pipeline.evaluate(ticker, as_of)
        metrics = calculate(ledger)
        stress_rows: list[dict] = []
        if include_stress and ablation == "full":
            costly = self.run(start=start, end=end, underlyings=names, ablation=ablation, cost_stress=2.0, include_stress=False)
            slippery = self.run(start=start, end=end, underlyings=names, ablation=ablation, slip_stress=2.0, include_stress=False)
            stress_rows.append(stress_note(metrics["net_pnl"], costly.metrics["net_pnl"], "cost_x2"))
            stress_rows.append(stress_note(metrics["net_pnl"], slippery.metrics["net_pnl"], "slippage_x2"))
        coverage = {
            "sessions": len(sessions),
            "expected_decisions": expected,
            "observed_decisions": len(ledger.decisions),
            "complete": len(ledger.decisions) == expected,
            "dataset": DATASET,
        }
        return BacktestResult(manifest, ledger, metrics, tuple(stress_rows), coverage)
