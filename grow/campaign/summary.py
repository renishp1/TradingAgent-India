"""Phase 10 — paper session summary artifact.

Aggregates campaign cycles + paper book into one auditable session report.
Paper-only; never a broker fill record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from grow.campaign.runner import CampaignCycleResult
from grow.decision.integration.contract import DecisionAction, IntegratedDecisionStatus
from grow.paper.engine import PaperExecutionEngine
from grow.paper.positions import PositionState


SESSION_SUMMARY_SCHEMA = "campaign.session_summary.v1"


@dataclass(frozen=True)
class PaperSessionSummary:
    """End-of-session (or mid-session) paper desk report."""

    session_id: str
    started_at: datetime
    ended_at: datetime | None
    status: str
    market_data_health: str
    provider_health: Mapping[str, Any]
    decisions: tuple[Mapping[str, Any], ...]
    rejected_decisions: tuple[Mapping[str, Any], ...]
    no_trade_reasons: tuple[Mapping[str, Any], ...]
    risk_guard_rejections: tuple[Mapping[str, Any], ...]
    trades: tuple[Mapping[str, Any], ...]
    entries: tuple[Mapping[str, Any], ...]
    exits: tuple[Mapping[str, Any], ...]
    mtm: Mapping[str, Any]
    gross_pnl: float
    charges: float
    net_pnl: float
    max_drawdown: float
    win_loss: Mapping[str, Any]
    agent_latency: tuple[Mapping[str, Any], ...]
    snapshot_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]
    cycle_ids: tuple[str, ...]
    position_summary: Mapping[str, Any]
    broker_order_calls: int = 0
    schema: str = SESSION_SUMMARY_SCHEMA
    paper_mode: bool = True
    live_trading: bool = False
    broker_order_path: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": None if self.ended_at is None else self.ended_at.isoformat(),
            "status": self.status,
            "market_data_health": self.market_data_health,
            "provider_health": dict(self.provider_health),
            "decisions": [dict(row) for row in self.decisions],
            "rejected_decisions": [dict(row) for row in self.rejected_decisions],
            "no_trade_reasons": [dict(row) for row in self.no_trade_reasons],
            "risk_guard_rejections": [dict(row) for row in self.risk_guard_rejections],
            "trades": [dict(row) for row in self.trades],
            "entries": [dict(row) for row in self.entries],
            "exits": [dict(row) for row in self.exits],
            "mtm": dict(self.mtm),
            "gross_pnl": self.gross_pnl,
            "charges": self.charges,
            "net_pnl": self.net_pnl,
            "max_drawdown": self.max_drawdown,
            "win_loss": dict(self.win_loss),
            "agent_latency": [dict(row) for row in self.agent_latency],
            "snapshot_ids": list(self.snapshot_ids),
            "decision_ids": list(self.decision_ids),
            "cycle_ids": list(self.cycle_ids),
            "position_summary": dict(self.position_summary),
            "broker_order_calls": 0,
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
        }


@dataclass
class _EquityPoint:
    timestamp: datetime
    equity: float


def build_session_summary(
    *,
    session_id: str,
    started_at: datetime,
    ended_at: datetime | None,
    status: str,
    cycles: Sequence[CampaignCycleResult],
    paper: PaperExecutionEngine,
    equity_curve: Sequence[_EquityPoint] = (),
    last_market_health: str = "UNKNOWN",
    last_provider_health: Mapping[str, Any] | None = None,
) -> PaperSessionSummary:
    """Assemble the Phase 10 session summary from recorded cycles + paper book."""
    decisions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    no_trade_reasons: list[dict[str, Any]] = []
    risk_rejections: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    latency: list[dict[str, Any]] = []
    snapshot_ids: list[str] = []
    decision_ids: list[str] = []
    cycle_ids: list[str] = []

    for cycle in cycles:
        decision = cycle.decision
        execution = cycle.execution
        snapshot_ids.append(cycle.snapshot_id)
        decision_ids.append(decision.decision_id)
        cycle_ids.append(cycle.cycle_id)
        row = {
            "decision_id": decision.decision_id,
            "cycle_id": cycle.cycle_id,
            "snapshot_id": cycle.snapshot_id,
            "status": decision.status.value,
            "action": decision.action.value,
            "reason_codes": list(decision.reason_codes),
            "risk_guard_result": decision.risk_guard_result,
            "risk_guard_reason": decision.risk_guard_reason,
            "execution_accepted": execution.accepted,
            "execution_reason": execution.reason,
        }
        decisions.append(row)
        if decision.status is IntegratedDecisionStatus.BLOCKED or (
            decision.risk_guard_result and decision.risk_guard_result != "APPROVED"
        ):
            risk_rejections.append(
                {
                    "decision_id": decision.decision_id,
                    "risk_guard_result": decision.risk_guard_result,
                    "risk_guard_reason": decision.risk_guard_reason,
                    "reason_codes": list(decision.reason_codes),
                }
            )
            rejected.append(row)
        if decision.action is DecisionAction.NO_TRADE:
            no_trade_reasons.append(
                {
                    "decision_id": decision.decision_id,
                    "reason_codes": list(decision.reason_codes),
                    "status": decision.status.value,
                    "risk_guard_reason": decision.risk_guard_reason,
                }
            )
            if decision.status is not IntegratedDecisionStatus.BLOCKED:
                rejected.append(row)
        if execution.accepted:
            entry = {
                "decision_id": decision.decision_id,
                "paper_order_id": execution.paper_order_id,
                "position_id": execution.position_id,
                "execution_price": execution.execution_price,
                "price_source": execution.price_source,
                "action": decision.action.value,
                "instrument": decision.candidate_instrument,
            }
            entries.append(entry)
            trades.append({**entry, "side": "BUY", "kind": "ENTRY"})
        for record in cycle.package.dispatch_records:
            latency.append(
                {
                    "cycle_id": cycle.cycle_id,
                    "agent_name": record.agent_name,
                    "agent_version": record.agent_version,
                    "execution_time_ms": record.execution_time_ms,
                    "status": record.status,
                    "accepted": record.accepted,
                }
            )

    exits: list[dict[str, Any]] = []
    wins = 0
    losses = 0
    flats = 0
    for position in paper.positions.all():
        if position.state is not PositionState.CLOSED:
            continue
        exit_row = {
            "position_id": position.position_id,
            "contract_id": position.contract_id,
            "exit_reason": position.exit_reason,
            "entry_price": position.entry_price,
            "exit_price": position.current_price,
            "realized_pnl": position.realized_pnl,
            "realized_gross": position.realized_gross,
            "total_costs": position.total_costs,
            "closed_at": None if position.closed_at is None else position.closed_at.isoformat(),
            "close_snapshot_id": position.close_snapshot_id,
        }
        exits.append(exit_row)
        trades.append({**exit_row, "side": "SELL", "kind": "EXIT"})
        if position.realized_pnl > 1e-9:
            wins += 1
        elif position.realized_pnl < -1e-9:
            losses += 1
        else:
            flats += 1

    book = paper.positions.summary()
    max_dd = _max_drawdown(equity_curve, starting_cash=book.starting_cash)
    provider = dict(last_provider_health or {})
    provider.setdefault("paper_mode", True)
    provider.setdefault("live_trading", False)
    provider.setdefault("broker_order_path", False)

    return PaperSessionSummary(
        session_id=session_id,
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        market_data_health=last_market_health,
        provider_health=provider,
        decisions=tuple(decisions),
        rejected_decisions=tuple(_unique_by_decision(rejected)),
        no_trade_reasons=tuple(no_trade_reasons),
        risk_guard_rejections=tuple(risk_rejections),
        trades=tuple(trades),
        entries=tuple(entries),
        exits=tuple(exits),
        mtm={
            "unrealized_pnl": book.unrealized_pnl,
            "open_exposure": book.open_exposure,
            "total_pnl": book.total_pnl,
            "valuation_gaps": book.valuation_gaps,
        },
        gross_pnl=book.gross_realized_pnl,
        charges=book.total_costs,
        net_pnl=book.net_realized_pnl,
        max_drawdown=max_dd,
        win_loss={"wins": wins, "losses": losses, "flats": flats, "closed_trades": wins + losses + flats},
        agent_latency=tuple(latency),
        snapshot_ids=tuple(dict.fromkeys(snapshot_ids)),
        decision_ids=tuple(dict.fromkeys(decision_ids)),
        cycle_ids=tuple(dict.fromkeys(cycle_ids)),
        position_summary=book.to_dict(),
        broker_order_calls=0,
    )


def _unique_by_decision(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.get("decision_id") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    return out


def _max_drawdown(curve: Sequence[_EquityPoint], *, starting_cash: float) -> float:
    """Peak-to-trough drawdown on session equity (cash + unrealized). Non-positive."""
    if not curve:
        return 0.0
    peak = starting_cash
    max_dd = 0.0
    for point in curve:
        peak = max(peak, point.equity)
        dd = point.equity - peak
        if dd < max_dd:
            max_dd = dd
    return round(max_dd, 4)


@dataclass
class SessionEquityTracker:
    """Track paper equity for max-drawdown in the session summary."""

    starting_cash: float
    points: list[_EquityPoint] = field(default_factory=list)

    def record(self, when: datetime, *, cash: float, unrealized: float) -> None:
        self.points.append(_EquityPoint(timestamp=when, equity=round(cash + unrealized, 4)))
