"""Phase 8 — single campaign runner: 4B → 4C → Risk → PaperExecutionEngine.

Canonical campaign path (no third executor):
  AgentMarketSnapshot
    → AnalysisOrchestrator (4B)
    → DecisionEngine (4C + SignalEngine; Risk Guard final authority)
    → PaperExecutionEngine (existing 3B paper fills)

Preserves ``LivePaperLoop`` as the legacy 3A path. Does not place broker orders.
Phase 9 adds optional durable paper checkpoints for cross-process restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grow.agents.base import SpecialistAgent
from grow.campaign.config import campaign_paper_config
from grow.clock import Clock, SystemClock
from grow.config import GrowConfig
from grow.decision.integration.contract import DecisionBookState, IntegratedDecision
from grow.decision.integration.engine import DecisionEngine
from grow.execution.lock import assert_paper_runtime
from grow.market_data.normalized.models import AgentMarketSnapshot
from grow.orchestration.cycle import AnalysisOrchestrator
from grow.orchestration.models import AggregateAnalysisPackage
from grow.paper.checkpoint import restore_paper_engine
from grow.paper.engine import PaperExecutionEngine, PaperExecutionResult
from grow.paper.fills import policy_from_config
from grow.risk.guard import RiskGuard


CAMPAIGN_RUNNER_VERSION = "campaign.runner.v1"


@dataclass(frozen=True)
class CampaignCycleResult:
    """One campaign cycle outcome. Paper-only; never a broker fill."""

    snapshot_id: str
    cycle_id: str
    package: AggregateAnalysisPackage
    decision: IntegratedDecision
    execution: PaperExecutionResult
    runner_version: str = CAMPAIGN_RUNNER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "cycle_id": self.cycle_id,
            "package_digest": self.package.package_digest,
            "decision_id": self.decision.decision_id,
            "decision_status": self.decision.status.value,
            "decision_action": self.decision.action.value,
            "campaign_signal": self.decision.campaign_signal,
            "execution": self.execution.to_dict(),
            "runner_version": self.runner_version,
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
        }


class CampaignRunner:
    """Wire existing 4B + 4C + paper engines into one campaign cycle."""

    def __init__(
        self,
        config: GrowConfig | None = None,
        *,
        clock: Clock | None = None,
        risk_guard: RiskGuard | None = None,
        risk_secret: str | None = None,
        specialists: tuple[SpecialistAgent, ...] | None = None,
        configured_strategies: tuple[str, ...] = ("trend",),
        apply_campaign_defaults: bool = True,
        checkpoint_path: Path | str | None = None,
        paper: PaperExecutionEngine | None = None,
        replay_store: Any | None = None,
    ) -> None:
        raw = config
        if apply_campaign_defaults:
            raw = campaign_paper_config(config)
        assert raw is not None
        assert_paper_runtime(raw.execution.mode, raw.execution.live_trading_enabled, "PAPER")
        self.config = raw
        # Production default is wall-clock IST. Tests inject FrozenClock explicitly.
        self.clock = clock if clock is not None else SystemClock()
        self.guard = risk_guard or RiskGuard(self.config, clock=self.clock, secret=risk_secret)
        self.orchestrator = AnalysisOrchestrator(
            specialists=specialists,
            configured_strategies=configured_strategies,
            execution_mode=self.config.execution.mode,
            live_trading=False,
        )
        self.decision_engine = DecisionEngine(
            self.config,
            risk_guard=self.guard,
            risk_secret=risk_secret,
        )
        self.checkpoint_path = None if checkpoint_path is None else Path(checkpoint_path)
        if paper is not None:
            self.paper = paper
            if self.checkpoint_path is not None:
                self.paper.checkpoint_path = self.checkpoint_path
        else:
            self.paper = PaperExecutionEngine(
                self.config,
                clock=self.clock,
                risk_guard=self.guard,
                risk_secret=risk_secret,
                checkpoint_path=self.checkpoint_path,
            )
        self._risk_secret = risk_secret
        self.replay_store = replay_store

    @classmethod
    def from_checkpoint(
        cls,
        path: Path | str,
        config: GrowConfig | None = None,
        *,
        clock: Clock | None = None,
        risk_secret: str | None = None,
        specialists: tuple[SpecialistAgent, ...] | None = None,
        configured_strategies: tuple[str, ...] = ("trend",),
        apply_campaign_defaults: bool = True,
    ) -> CampaignRunner:
        """Cross-process campaign restart from a durable paper checkpoint."""
        raw = config
        if apply_campaign_defaults:
            raw = campaign_paper_config(config)
        assert raw is not None
        resolved_clock = clock if clock is not None else SystemClock()
        paper = restore_paper_engine(
            raw,
            path,
            clock=resolved_clock,
            risk_secret=risk_secret,
        )
        paper.checkpoint_path = Path(path)
        return cls(
            raw,
            clock=resolved_clock,
            risk_guard=paper.guard,
            risk_secret=risk_secret,
            specialists=specialists,
            configured_strategies=configured_strategies,
            apply_campaign_defaults=False,
            checkpoint_path=path,
            paper=paper,
        )

    @property
    def fill_policy_version(self) -> str:
        return policy_from_config(self.config).version

    def run_cycle(
        self,
        snapshot: AgentMarketSnapshot,
        *,
        cycle_id: str | None = None,
        book: DecisionBookState | None = None,
    ) -> CampaignCycleResult:
        """Full campaign path: 4B → DecisionEngine → PaperExecutionEngine."""
        if snapshot.live_trading or not snapshot.paper_mode:
            raise ValueError("campaign runner requires a paper-only snapshot")
        package = self.orchestrator.run(snapshot, cycle_id=cycle_id)
        return self.run_from_package(snapshot, package, book=book)

    def run_from_package(
        self,
        snapshot: AgentMarketSnapshot,
        package: AggregateAnalysisPackage,
        *,
        book: DecisionBookState | None = None,
    ) -> CampaignCycleResult:
        """Skip 4B when a package already exists; still DecisionEngine → paper."""
        if snapshot.live_trading or not snapshot.paper_mode:
            raise ValueError("campaign runner requires a paper-only snapshot")
        decision = self.decision_engine.decide(snapshot=snapshot, package=package, book=book)
        execution = self.paper.execute(decision, snapshot, package=package)
        result = CampaignCycleResult(
            snapshot_id=snapshot.snapshot_id,
            cycle_id=package.cycle_id,
            package=package,
            decision=decision,
            execution=execution,
        )
        if self.replay_store is not None:
            book_payload = None if book is None else book.to_dict()
            self.replay_store.record_cycle(result, snapshot, book=book_payload)
        return result

    def on_snapshot(self, snapshot: AgentMarketSnapshot) -> tuple[str, ...]:
        """Mark / timeout / recovery on the shared paper engine."""
        if snapshot.live_trading or not snapshot.paper_mode:
            raise ValueError("campaign runner requires a paper-only snapshot")
        reasons = self.paper.on_snapshot(snapshot)
        if self.replay_store is not None:
            from grow.campaign.replay import sync_exits_from_paper

            sync_exits_from_paper(self.replay_store, self.paper)
        return reasons
