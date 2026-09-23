"""Phase 10 — complete paper-trading session runner.

Safe end-to-end session over the existing campaign path:

  START SESSION
    → CHECK MARKET
    → (optional) ingest fixture / live snapshots — no broker
    → CREATE SNAPSHOT (caller-supplied AgentMarketSnapshot)
    → RUN 4B → 4C → Risk Guard → PaperExecutionEngine
    → MONITOR POSITION (on_snapshot marks / exits)
    → JOURNAL (paper journal + optional durable checkpoint)
    → SESSION SUMMARY

Does not place broker orders. Does not invent a third executor.
``LivePaperLoop`` remains the legacy 3A path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from grow.agents.base import SpecialistAgent
from grow.campaign.config import campaign_paper_config
from grow.campaign.runner import CampaignCycleResult, CampaignRunner
from grow.campaign.summary import (
    SESSION_SUMMARY_SCHEMA,
    PaperSessionSummary,
    SessionEquityTracker,
    build_session_summary,
)
from grow.clock import Clock, SystemClock
from grow.config import GrowConfig
from grow.decision.integration.contract import DecisionBookState, digest_payload
from grow.errors import GrowSafetyError
from grow.execution.lock import assert_paper_runtime
from grow.market_data.normalized.models import AgentMarketSnapshot, DataQualityStatus
from grow.market_data.snapshots.builder import gate_snapshot_quality
from grow.paper.engine import PaperExecutionEngine
from grow.risk.guard import RiskGuard


SESSION_RUNNER_VERSION = "campaign.session.v1"


@dataclass(frozen=True)
class MarketCheckResult:
    ok: bool
    health: str
    provider_health: Mapping[str, Any]
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "health": self.health,
            "provider_health": dict(self.provider_health),
            "reason": self.reason,
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
        }


class PaperSessionRunner:
    """Multi-cycle paper session over ``CampaignRunner`` + session summary."""

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
        campaign: CampaignRunner | None = None,
        session_id: str | None = None,
    ) -> None:
        raw = config
        if apply_campaign_defaults and campaign is None:
            raw = campaign_paper_config(config)
        if campaign is not None:
            self.campaign = campaign
            self.config = campaign.config
            self.clock = campaign.clock
        else:
            assert raw is not None
            assert_paper_runtime(raw.execution.mode, raw.execution.live_trading_enabled, "PAPER")
            self.config = raw
            self.clock = clock if clock is not None else SystemClock()
            self.campaign = CampaignRunner(
                raw,
                clock=self.clock,
                risk_guard=risk_guard,
                risk_secret=risk_secret,
                specialists=specialists,
                configured_strategies=configured_strategies,
                apply_campaign_defaults=False,
                checkpoint_path=checkpoint_path,
            )
        self.session_id = session_id or (
            "session-"
            + digest_payload(
                {
                    "paper_session": self.campaign.paper.session_id,
                    "nonce": uuid4().hex[:8],
                }
            )[:16]
        )
        self._cycles: list[CampaignCycleResult] = []
        self._monitor_events: list[dict[str, Any]] = []
        self._started_at = None
        self._ended_at = None
        self._status = "CREATED"
        self._last_market_health = "UNKNOWN"
        self._last_provider_health: dict[str, Any] = {
            "provider": "unspecified",
            "paper_mode": True,
            "live_trading": False,
        }
        self._equity = SessionEquityTracker(starting_cash=float(self.config.paper.starting_cash))
        self.runner_version = SESSION_RUNNER_VERSION

    @property
    def paper(self) -> PaperExecutionEngine:
        return self.campaign.paper

    @property
    def status(self) -> str:
        return self._status

    @property
    def cycles(self) -> tuple[CampaignCycleResult, ...]:
        return tuple(self._cycles)

    def start(self) -> str:
        """Begin a paper-only session. Rejects live/broker configuration."""
        if self._status not in {"CREATED", "ENDED"}:
            raise GrowSafetyError(f"paper session cannot start from status={self._status}")
        assert_paper_runtime(self.config.execution.mode, self.config.execution.live_trading_enabled, "PAPER")
        if self.config.execution.live_trading_enabled or self.config.live_data.live_trading:
            raise GrowSafetyError("paper session runner forbids live trading")
        if not self.config.live_data.paper_mode:
            raise GrowSafetyError("paper session runner requires paper_mode")
        self._started_at = self.clock.now()
        self._ended_at = None
        self._status = "RUNNING"
        self._cycles.clear()
        self._monitor_events.clear()
        self._equity = SessionEquityTracker(starting_cash=float(self.config.paper.starting_cash))
        self._equity.record(self._started_at, cash=float(self.paper.ledger.book.cash), unrealized=0.0)
        return self.session_id

    def check_market(self, snapshot: AgentMarketSnapshot) -> MarketCheckResult:
        """Map snapshot quality into a session market-health check."""
        if snapshot.live_trading or not snapshot.paper_mode:
            raise ValueError("paper session requires a paper-only snapshot")
        quality = gate_snapshot_quality(snapshot)
        health = quality.value
        provider = {
            "provider": snapshot.provider,
            "data_quality": quality.value,
            "snapshot_id": snapshot.snapshot_id,
            "as_of": snapshot.decision_timestamp.isoformat(),
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
            "diagnostics": dict(snapshot.diagnostics or {}),
        }
        self._last_market_health = health
        self._last_provider_health = provider
        ok = quality is DataQualityStatus.OK
        return MarketCheckResult(
            ok=ok,
            health=health,
            provider_health=provider,
            reason=None if ok else f"DATA_{health}",
        )

    def process_snapshot(
        self,
        snapshot: AgentMarketSnapshot,
        *,
        cycle_id: str | None = None,
        book: DecisionBookState | None = None,
        decide: bool = True,
        monitor: bool = True,
    ) -> dict[str, Any]:
        """One session step: market check → optional decide cycle → optional monitor."""
        if self._status != "RUNNING":
            raise GrowSafetyError("paper session is not RUNNING")
        if snapshot.live_trading or not snapshot.paper_mode:
            raise ValueError("paper session requires a paper-only snapshot")

        market = self.check_market(snapshot)
        monitor_reasons: tuple[str, ...] = ()
        if monitor:
            monitor_reasons = self.campaign.on_snapshot(snapshot)
            self._monitor_events.append(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "reasons": list(monitor_reasons),
                    "as_of": snapshot.decision_timestamp.isoformat(),
                }
            )

        cycle: CampaignCycleResult | None = None
        if decide:
            if not market.ok:
                # Fail closed on new decisions when market health is not OK.
                cycle = None
            else:
                cycle = self.campaign.run_cycle(snapshot, cycle_id=cycle_id, book=book)
                self._cycles.append(cycle)

        self._record_equity(snapshot)
        return {
            "session_id": self.session_id,
            "market": market.to_dict(),
            "monitor_reasons": list(monitor_reasons),
            "cycle": None if cycle is None else cycle.to_dict(),
            "skipped_decision": decide and not market.ok,
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
            "broker_order_calls": 0,
        }

    def monitor_only(self, snapshot: AgentMarketSnapshot) -> tuple[str, ...]:
        """MTM / exit / timeout recovery without a new decision cycle."""
        result = self.process_snapshot(snapshot, decide=False, monitor=True)
        return tuple(result["monitor_reasons"])

    def end(self) -> PaperSessionSummary:
        """Close the session and emit the summary artifact."""
        if self._status != "RUNNING":
            raise GrowSafetyError("paper session is not RUNNING")
        assert self._started_at is not None
        self._ended_at = self.clock.now()
        self._status = "ENDED"
        # Final equity point + durable checkpoint if configured.
        self._record_equity_now()
        self.paper.checkpoint()
        return self.summary()

    def summary(self) -> PaperSessionSummary:
        """Build the current session summary (works while RUNNING or after END)."""
        if self._started_at is None:
            raise GrowSafetyError("paper session has not started")
        return build_session_summary(
            session_id=self.session_id,
            started_at=self._started_at,
            ended_at=self._ended_at,
            status=self._status,
            cycles=self._cycles,
            paper=self.paper,
            equity_curve=self._equity.points,
            last_market_health=self._last_market_health,
            last_provider_health=self._last_provider_health,
        )

    def write_summary(self, path: Path | str) -> Path:
        """Persist the session summary JSON artifact."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = self.summary().to_dict()
        target.write_text(json.dumps(body, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        return target

    def _record_equity(self, snapshot: AgentMarketSnapshot) -> None:
        book = self.paper.positions.summary()
        self._equity.record(
            snapshot.decision_timestamp,
            cash=float(self.paper.ledger.book.cash),
            unrealized=float(book.unrealized_pnl),
        )

    def _record_equity_now(self) -> None:
        book = self.paper.positions.summary()
        self._equity.record(
            self.clock.now(),
            cash=float(self.paper.ledger.book.cash),
            unrealized=float(book.unrealized_pnl),
        )


__all__ = [
    "SESSION_RUNNER_VERSION",
    "SESSION_SUMMARY_SCHEMA",
    "MarketCheckResult",
    "PaperSessionRunner",
    "PaperSessionSummary",
]
