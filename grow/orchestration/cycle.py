"""Analysis-cycle orchestrator — Requirement 4B intelligence layer."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from grow.agents.base import SpecialistAgent
from grow.agents.market_data import MarketDataAgent
from grow.agents.options import OptionsChainAgent
from grow.agents.regime import RegimeAgent
from grow.agents.strategy_research import StrategyResearchAgent
from grow.agents.technical import TechnicalAgent
from grow.execution.lock import assert_paper_runtime
from grow.market_data.normalized.models import AgentMarketSnapshot
from grow.market_data.snapshots.builder import gate_snapshot_quality
from grow.orchestration.aggregator import aggregate_outputs
from grow.orchestration.dispatcher import dispatch_agents
from grow.orchestration.models import AggregateAnalysisPackage


@dataclass
class AnalysisCycleStore:
    """Append-only persistence for analysis packages (audit + replay)."""

    packages: list[AggregateAnalysisPackage] = field(default_factory=list)
    snapshots: dict[str, AgentMarketSnapshot] = field(default_factory=dict)

    def save(self, package: AggregateAnalysisPackage, snapshot: AgentMarketSnapshot) -> None:
        self.packages.append(package)
        self.snapshots[package.cycle_id] = snapshot

    def get(self, cycle_id: str) -> AggregateAnalysisPackage | None:
        for row in self.packages:
            if row.cycle_id == cycle_id:
                return row
        return None

    def get_snapshot(self, cycle_id: str) -> AgentMarketSnapshot | None:
        return self.snapshots.get(cycle_id)


class AnalysisOrchestrator:
    """Canonical 4B intelligence pipeline over one immutable snapshot.

    Dispatch (concurrent, bounded wait) → validate → conflict-preserving
    aggregate → optional replay. Paper-only; no broker order path.
    """

    def __init__(
        self,
        *,
        specialists: tuple[SpecialistAgent, ...] | None = None,
        configured_strategies: tuple[str, ...] = (),
        agent_timeout_seconds: float = 2.0,
        store: AnalysisCycleStore | None = None,
        execution_mode: str = "paper",
        live_trading: bool = False,
    ) -> None:
        assert_paper_runtime(execution_mode, live_trading, "PAPER")
        self.agent_timeout_seconds = agent_timeout_seconds
        self.store = store or AnalysisCycleStore()
        if specialists is None:
            specialists = (
                MarketDataAgent(),
                TechnicalAgent(),
                OptionsChainAgent(),
                RegimeAgent(),
                StrategyResearchAgent(configured_strategies),
            )
        self.specialists = specialists

    def run(self, snapshot: AgentMarketSnapshot, *, cycle_id: str | None = None) -> AggregateAnalysisPackage:
        assert_paper_runtime("paper", False, "PAPER")
        if snapshot.live_trading or not snapshot.paper_mode:
            raise ValueError("snapshot must remain paper-only")
        cycle = cycle_id or f"cycle-{uuid.uuid4().hex[:12]}"
        quality = gate_snapshot_quality(snapshot)
        if quality.value in {"INSUFFICIENT", "REJECTED", "STALE"}:
            package = aggregate_outputs(
                cycle_id=cycle,
                snapshot_id=snapshot.snapshot_id,
                snapshot_version=snapshot.version,
                as_of=snapshot.decision_timestamp,
                outputs=(),
                rejected_outputs=(
                    {
                        "agent_name": "orchestrator",
                        "reason": f"SNAPSHOT_INVALID:{quality.value}",
                        "quality_notes": list(snapshot.quality_notes),
                    },
                ),
                dispatch_records=(),
            )
            # Replace summary for invalid snapshot stop-before-dispatch.
            package = AggregateAnalysisPackage(
                cycle_id=package.cycle_id,
                snapshot_id=package.snapshot_id,
                snapshot_version=package.snapshot_version,
                as_of=package.as_of,
                agent_outputs=(),
                rejected_outputs=package.rejected_outputs,
                dispatch_records=(),
                conflicts=("SNAPSHOT_INVALID",),
                supporting_evidence=(),
                conflicting_evidence=(),
                unavailable_agents=tuple(a.agent_name for a in self.specialists),
                debate=package.debate,
                cycle_summary=f"STOPPED_BEFORE_DISPATCH|{quality.value}",
                package_digest=package.package_digest,
            )
            self.store.save(package, snapshot)
            return package

        outputs, rejected, records = dispatch_agents(
            cycle_id=cycle,
            snapshot=snapshot,
            specialists=self.specialists,
            timeout_seconds=self.agent_timeout_seconds,
        )
        package = aggregate_outputs(
            cycle_id=cycle,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            as_of=snapshot.decision_timestamp,
            outputs=outputs,
            rejected_outputs=rejected,
            dispatch_records=records,
        )
        self.store.save(package, snapshot)
        return package

    def replay(self, cycle_id: str) -> AggregateAnalysisPackage:
        """Re-run specialists against the stored immutable snapshot."""
        snapshot = self.store.get_snapshot(cycle_id)
        if snapshot is None:
            raise KeyError(f"unknown cycle_id:{cycle_id}")
        # Deterministic cycle id for replay provenance.
        replay_id = f"replay-{cycle_id}"
        return self.run(snapshot, cycle_id=replay_id)


def stable_cycle_id(snapshot_id: str, as_of: datetime) -> str:
    raw = f"{snapshot_id}:{as_of.isoformat()}"
    return "cycle-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
