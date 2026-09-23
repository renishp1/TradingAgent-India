"""Phase 13 — multi-session real-market paper campaign infrastructure.

Runs a meaningful number of NSE paper sessions over the existing
``PaperSessionRunner`` / ``CampaignRunner`` / ``PaperExecutionEngine`` path.

Each session emits an EOD report. The campaign never places broker orders.
LIVE-PAPER and FIXTURE evaluation labels are supported; they are never mixed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from grow.agents.base import SpecialistAgent
from grow.campaign.config import campaign_paper_config
from grow.campaign.eod import SessionEODReport, build_session_eod_report
from grow.campaign.replay import TradeReplayStore
from grow.campaign.runner import CampaignRunner
from grow.campaign.session import PaperSessionRunner
from grow.clock import Clock, FrozenClock, SystemClock
from grow.config import GrowConfig, load_config
from grow.decision.integration.contract import digest_payload
from grow.errors import GrowConfigError, GrowSafetyError
from grow.execution.lock import assert_paper_runtime
from grow.market_data.normalized.models import AgentMarketSnapshot
from grow.market_data.provenance import MarketDataSource
from grow.validation.labels import (
    EvaluationLabel,
    assert_no_fixture_profitability_claim,
    assert_single_evaluation_label,
    label_payload,
    parse_evaluation_label,
)


PAPER_CAMPAIGN_VERSION = "campaign.paper.multi_session.v1"
CAMPAIGN_REPORT_SCHEMA = "campaign.paper_campaign_report.v1"

SessionClockFactory = Callable[[date, Sequence[AgentMarketSnapshot]], Clock]


@dataclass(frozen=True)
class SessionFeed:
    """One NSE session's chronological paper snapshots (caller-supplied)."""

    session_date: date
    snapshots: tuple[AgentMarketSnapshot, ...]
    reconnects: tuple[Mapping[str, Any], ...] = ()
    data_failures: tuple[Mapping[str, Any], ...] = ()
    stale_events: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshots", tuple(self.snapshots))
        object.__setattr__(self, "reconnects", tuple(dict(row) for row in self.reconnects))
        object.__setattr__(self, "data_failures", tuple(dict(row) for row in self.data_failures))
        object.__setattr__(self, "stale_events", tuple(dict(row) for row in self.stale_events))
        if not self.snapshots:
            raise GrowConfigError("PAPER_CAMPAIGN_EMPTY_SESSION")
        days = {snap.session_date for snap in self.snapshots}
        if days != {self.session_date}:
            raise GrowConfigError("PAPER_CAMPAIGN_SESSION_DATE_MISMATCH")
        ordered = tuple(
            sorted(self.snapshots, key=lambda snap: snap.decision_timestamp)
        )
        object.__setattr__(self, "snapshots", ordered)


@dataclass(frozen=True)
class PaperCampaignReport:
    """Roll-up across all sessions in one paper campaign.

    Each session uses a fresh paper engine, so drawdown is not continuous capital.
    ``worst_session_drawdown`` is the most negative single-session peak-to-trough
    drawdown (min of session ``max_drawdown`` values), not a combined equity curve.
    """

    campaign_id: str
    evaluation_label: str
    sessions: tuple[SessionEODReport, ...]
    session_count: int
    total_decisions: int
    total_trades: int
    total_rejected: int
    combined_net_pnl: float
    worst_session_drawdown: float
    eod_paths: tuple[str, ...]
    profitability_claim: bool = False
    broker_order_calls: int = 0
    schema: str = CAMPAIGN_REPORT_SCHEMA
    paper_mode: bool = True
    live_trading: bool = False
    broker_order_path: bool = False

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema": self.schema,
            "campaign_id": self.campaign_id,
            "evaluation_label": self.evaluation_label,
            "session_count": self.session_count,
            "sessions": [row.to_dict() for row in self.sessions],
            "total_decisions": self.total_decisions,
            "total_trades": self.total_trades,
            "total_rejected": self.total_rejected,
            "combined_net_pnl": self.combined_net_pnl,
            "worst_session_drawdown": self.worst_session_drawdown,
            "eod_paths": list(self.eod_paths),
            "profitability_claim": False,
            "broker_order_calls": 0,
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
            "campaign_version": PAPER_CAMPAIGN_VERSION,
        }
        body.update(label_payload(self.evaluation_label))
        body["profitability_claim"] = False
        return body


def _default_clock_factory(session_date: date, snapshots: Sequence[AgentMarketSnapshot]) -> Clock:
    if snapshots:
        return FrozenClock(snapshots[0].decision_timestamp)
    return SystemClock()


def assert_campaign_snapshot_label(
    evaluation_label: str | EvaluationLabel,
    snapshots: Sequence[AgentMarketSnapshot],
) -> EvaluationLabel:
    """Fail closed when LIVE-PAPER / FIXTURE snapshot provenance does not match the label."""

    label = parse_evaluation_label(evaluation_label)
    if label in {EvaluationLabel.HISTORICAL, EvaluationLabel.SYNTHETIC}:
        raise GrowConfigError(f"PAPER_CAMPAIGN_LABEL_FORBIDDEN:{label.value}")
    if not snapshots:
        raise GrowConfigError("PAPER_CAMPAIGN_EMPTY_SESSION")

    sources = []
    for snap in snapshots:
        if snap.live_trading or not snap.paper_mode:
            raise GrowSafetyError("paper campaign requires paper-only snapshots")
        source = snap.market_data_source
        if not isinstance(source, MarketDataSource):
            source = MarketDataSource(str(source))
        sources.append(source)
        if source is MarketDataSource.MIXED:
            raise GrowConfigError("MIXED_MARKET_DATA_SOURCE")

    unique = {item.value for item in sources}
    if len(unique) > 1:
        raise GrowConfigError(f"PAPER_CAMPAIGN_SNAPSHOT_PROVENANCE_MIXED:{','.join(sorted(unique))}")

    only = sources[0]
    if label is EvaluationLabel.LIVE_PAPER:
        if only is MarketDataSource.FIXTURE or any(snap.is_fixture for snap in snapshots):
            raise GrowConfigError("LIVE_PAPER_FIXTURE_SNAPSHOT_FORBIDDEN")
        if only is not MarketDataSource.LIVE:
            raise GrowConfigError(f"LIVE_PAPER_REQUIRES_LIVE_PROVENANCE:{only.value}")
    elif label is EvaluationLabel.FIXTURE:
        if only is not MarketDataSource.FIXTURE and not all(snap.is_fixture for snap in snapshots):
            raise GrowConfigError("FIXTURE_CAMPAIGN_REQUIRES_FIXTURE_SNAPSHOTS")
    return label


class PaperCampaign:
    """Multi-session paper campaign over existing session + campaign runners."""

    def __init__(
        self,
        config: GrowConfig | None = None,
        *,
        risk_secret: str,
        evaluation_label: str | EvaluationLabel = EvaluationLabel.LIVE_PAPER,
        artifact_dir: Path | str | None = None,
        specialists: tuple[SpecialistAgent, ...] | None = None,
        configured_strategies: tuple[str, ...] = ("trend",),
        apply_campaign_defaults: bool = True,
        replay_store: TradeReplayStore | None = None,
        campaign_id: str | None = None,
        clock_factory: SessionClockFactory | None = None,
    ) -> None:
        raw = campaign_paper_config(config) if apply_campaign_defaults else (config or load_config())
        assert_paper_runtime(raw.execution.mode, raw.execution.live_trading_enabled, "PAPER")
        if raw.execution.live_trading_enabled or raw.live_data.live_trading:
            raise GrowSafetyError("paper campaign forbids live trading")
        if not raw.live_data.paper_mode:
            raise GrowSafetyError("paper campaign requires paper_mode")

        label = parse_evaluation_label(evaluation_label)
        if label in {EvaluationLabel.HISTORICAL, EvaluationLabel.SYNTHETIC}:
            raise GrowConfigError(f"PAPER_CAMPAIGN_LABEL_FORBIDDEN:{label.value}")
        assert_no_fixture_profitability_claim(label, profitability_claim=False)

        self.config = raw
        self.risk_secret = risk_secret
        self.evaluation_label = label
        self.specialists = specialists
        self.configured_strategies = configured_strategies
        self.replay_store = replay_store
        self.clock_factory = clock_factory or _default_clock_factory
        self.artifact_dir = None if artifact_dir is None else Path(artifact_dir)
        if self.artifact_dir is not None:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.campaign_id = campaign_id or (
            "campaign-"
            + digest_payload({"paper_campaign": PAPER_CAMPAIGN_VERSION, "nonce": uuid4().hex[:8]})[:16]
        )
        self._eods: list[SessionEODReport] = []
        self._eod_paths: list[str] = []
        self._status = "CREATED"
        self.campaign_version = PAPER_CAMPAIGN_VERSION

    @property
    def status(self) -> str:
        return self._status

    @property
    def eod_reports(self) -> tuple[SessionEODReport, ...]:
        return tuple(self._eods)

    def run_session(self, feed: SessionFeed) -> SessionEODReport:
        """Run one paper session and emit its EOD report."""

        if self._status == "ENDED":
            raise GrowSafetyError(
                "paper campaign has ended; construct a fresh PaperCampaign for another run"
            )

        assert_single_evaluation_label([self.evaluation_label])
        assert_campaign_snapshot_label(self.evaluation_label, feed.snapshots)

        if self._status == "CREATED":
            self._status = "RUNNING"

        clock = self.clock_factory(feed.session_date, feed.snapshots)
        campaign = CampaignRunner(
            self.config,
            clock=clock,
            risk_secret=self.risk_secret,
            specialists=self.specialists,
            configured_strategies=self.configured_strategies,
            apply_campaign_defaults=False,
            replay_store=self.replay_store,
        )
        runner = PaperSessionRunner(
            self.config,
            clock=clock,
            risk_secret=self.risk_secret,
            specialists=self.specialists,
            configured_strategies=self.configured_strategies,
            apply_campaign_defaults=False,
            campaign=campaign,
        )
        runner.start()
        for index, snapshot in enumerate(feed.snapshots):
            if isinstance(clock, FrozenClock):
                clock._when = snapshot.decision_timestamp
            runner.process_snapshot(
                snapshot,
                cycle_id=f"{self.campaign_id}-{feed.session_date.isoformat()}-{index:03d}",
            )
        summary = runner.end()
        eod = build_session_eod_report(
            campaign_id=self.campaign_id,
            session_date=feed.session_date,
            evaluation_label=self.evaluation_label,
            summary=summary,
            cycles=runner.cycles,
            snapshots=feed.snapshots,
            paper=runner.paper,
            monitor_events=runner.monitor_events,
            data_failures=feed.data_failures,
            reconnects=feed.reconnects,
            stale_events=feed.stale_events,
        )
        path = self._write_eod(eod)
        if path is not None:
            self._eod_paths.append(str(path))
        self._eods.append(eod)
        return eod

    def run(self, feeds: Sequence[SessionFeed]) -> PaperCampaignReport:
        """Run multiple NSE paper sessions chronologically and roll up results.

        One-shot lifecycle (Phase 10 session pattern): CREATED → RUNNING → ENDED.
        A second ``run()`` on the same instance is rejected; construct a fresh
        ``PaperCampaign`` instead of resetting accumulated EODs.
        """

        if self._status != "CREATED":
            raise GrowSafetyError(
                f"paper campaign cannot run from status={self._status}; "
                "construct a fresh PaperCampaign"
            )
        if self._eods or self._eod_paths:
            raise GrowSafetyError(
                "paper campaign already has session EODs; construct a fresh PaperCampaign"
            )
        if not feeds:
            raise GrowConfigError("PAPER_CAMPAIGN_NO_SESSIONS")
        ordered = tuple(sorted(feeds, key=lambda feed: feed.session_date))
        dates = [feed.session_date for feed in ordered]
        if len(dates) != len(set(dates)):
            raise GrowConfigError("PAPER_CAMPAIGN_DUPLICATE_SESSION_DATE")

        # Label purity across the whole campaign (inspect all snapshots up front).
        all_snaps = [snap for feed in ordered for snap in feed.snapshots]
        assert_campaign_snapshot_label(self.evaluation_label, all_snaps)

        self._status = "RUNNING"
        for feed in ordered:
            self.run_session(feed)

        report = self.report()
        self._status = "ENDED"
        return report

    def report(self) -> PaperCampaignReport:
        if not self._eods:
            raise GrowConfigError("PAPER_CAMPAIGN_NO_EOD")
        assert_no_fixture_profitability_claim(self.evaluation_label, profitability_claim=False)
        combined_net = round(sum(float(eod.summary.get("net_pnl", 0.0)) for eod in self._eods), 4)
        # Per-session engines: take the worst (most negative) single-session drawdown.
        worst_dd = round(min(float(eod.summary.get("max_drawdown", 0.0)) for eod in self._eods), 4)
        return PaperCampaignReport(
            campaign_id=self.campaign_id,
            evaluation_label=self.evaluation_label.value,
            sessions=tuple(self._eods),
            session_count=len(self._eods),
            total_decisions=sum(int(eod.telemetry.get("decision_count", 0)) for eod in self._eods),
            total_trades=sum(int(eod.telemetry.get("trade_count", 0)) for eod in self._eods),
            total_rejected=sum(int(eod.telemetry.get("rejected_decision_count", 0)) for eod in self._eods),
            combined_net_pnl=combined_net,
            worst_session_drawdown=worst_dd,
            eod_paths=tuple(self._eod_paths),
            profitability_claim=False,
            broker_order_calls=0,
        )

    def write_report(self, path: Path | str | None = None) -> Path:
        target = Path(path) if path is not None else (
            (self.artifact_dir or Path(".")) / f"{self.campaign_id}-campaign.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        body = self.report().to_dict()
        target.write_text(json.dumps(body, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        return target

    def _write_eod(self, eod: SessionEODReport) -> Path | None:
        if self.artifact_dir is None:
            return None
        target = self.artifact_dir / f"{self.campaign_id}-{eod.session_date.isoformat()}-eod.json"
        target.write_text(json.dumps(eod.to_dict(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        return target


__all__ = [
    "CAMPAIGN_REPORT_SCHEMA",
    "PAPER_CAMPAIGN_VERSION",
    "PaperCampaign",
    "PaperCampaignReport",
    "SessionFeed",
    "assert_campaign_snapshot_label",
]
