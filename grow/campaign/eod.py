"""Phase 13 — end-of-day (EOD) session report for the paper campaign.

Answers the operational questions for one NSE paper session without claiming
profitability. Paper-only; never a broker fill record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from grow.campaign.runner import CampaignCycleResult
from grow.campaign.summary import PaperSessionSummary
from grow.decision.integration.contract import DecisionAction, IntegratedDecisionStatus
from grow.market_data.normalized.models import AgentMarketSnapshot, DataQualityStatus
from grow.paper.engine import PaperExecutionEngine
from grow.validation.labels import EvaluationLabel, label_payload, parse_evaluation_label


EOD_SCHEMA = "campaign.eod_report.v1"


@dataclass(frozen=True)
class SessionEODReport:
    """One session EOD artifact for the multi-session paper campaign."""

    campaign_id: str
    session_id: str
    session_date: date
    evaluation_label: str
    summary: Mapping[str, Any]
    answers: Mapping[str, Any]
    telemetry: Mapping[str, Any]
    started_at: datetime
    ended_at: datetime | None
    status: str
    profitability_claim: bool = False
    broker_order_calls: int = 0
    schema: str = EOD_SCHEMA
    paper_mode: bool = True
    live_trading: bool = False
    broker_order_path: bool = False

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema": self.schema,
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "session_date": self.session_date.isoformat(),
            "evaluation_label": self.evaluation_label,
            "summary": dict(self.summary),
            "answers": dict(self.answers),
            "telemetry": dict(self.telemetry),
            "started_at": self.started_at.isoformat(),
            "ended_at": None if self.ended_at is None else self.ended_at.isoformat(),
            "status": self.status,
            "profitability_claim": False,
            "broker_order_calls": 0,
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
        }
        body.update(label_payload(self.evaluation_label))
        body["profitability_claim"] = False
        return body


def _quote_book(snapshot: AgentMarketSnapshot | None, instrument: str | None) -> dict[str, Any]:
    if snapshot is None or not instrument:
        return {}
    for row in snapshot.option_contracts:
        if row.provider_contract_id == instrument:
            spread = None
            if row.bid is not None and row.ask is not None:
                spread = round(float(row.ask) - float(row.bid), 4)
            return {
                "bid": row.bid,
                "ask": row.ask,
                "ltp": row.ltp,
                "spread": spread,
                "quote_timestamp": row.quote_timestamp.isoformat(),
            }
    return {}


def build_session_eod_report(
    *,
    campaign_id: str,
    session_date: date,
    evaluation_label: str | EvaluationLabel,
    summary: PaperSessionSummary,
    cycles: Sequence[CampaignCycleResult],
    snapshots: Sequence[AgentMarketSnapshot] = (),
    paper: PaperExecutionEngine | None = None,
    monitor_events: Sequence[Mapping[str, Any]] = (),
    data_failures: Sequence[Mapping[str, Any]] = (),
    reconnects: Sequence[Mapping[str, Any]] = (),
    stale_events: Sequence[Mapping[str, Any]] = (),
) -> SessionEODReport:
    """Assemble the Phase 13 EOD report that answers why/what for one session."""

    label = parse_evaluation_label(evaluation_label)
    snaps_by_id = {snap.snapshot_id: snap for snap in snapshots}
    why_traded: list[dict[str, Any]] = []
    why_not: list[dict[str, Any]] = []
    why_rejected: list[dict[str, Any]] = []
    agent_statements: list[dict[str, Any]] = []
    risk_statements: list[dict[str, Any]] = []
    prices_used: list[dict[str, Any]] = []
    slippage_total = 0.0
    spread_total = 0.0
    risk_blocks = 0
    agent_latency: list[dict[str, Any]] = []
    decision_latency: list[dict[str, Any]] = []

    for cycle in cycles:
        decision = cycle.decision
        execution = cycle.execution
        snap = snaps_by_id.get(cycle.snapshot_id)
        instrument = decision.candidate_instrument
        if decision.trade_candidate is not None:
            instrument = decision.trade_candidate.instrument
        quotes = _quote_book(snap, instrument)
        decision_row = {
            "decision_id": decision.decision_id,
            "cycle_id": cycle.cycle_id,
            "snapshot_id": cycle.snapshot_id,
            "status": decision.status.value,
            "action": decision.action.value,
            "reason_codes": list(decision.reason_codes),
            "risk_guard_reason": decision.risk_guard_reason,
            "risk_guard_result": decision.risk_guard_result,
            "instrument": instrument,
        }

        traded = (
            decision.action in {DecisionAction.BUY_CE, DecisionAction.BUY_PE}
            and execution.accepted
        )
        if traded:
            signal = dict(decision.campaign_signal or {})
            why_traded.append(
                {
                    **decision_row,
                    "entry_reason": signal.get("entry_reason") or signal.get("rationale"),
                    "supporting_findings": list(decision.supporting_findings),
                    "fill_price": execution.execution_price,
                    "price_source": execution.price_source,
                    "paper_order_id": execution.paper_order_id,
                    "position_id": execution.position_id,
                }
            )
            price_row = {
                "decision_id": decision.decision_id,
                "instrument": instrument,
                "fill_price": execution.execution_price,
                "price_source": execution.price_source,
                "limit_price": None
                if decision.trade_candidate is None
                else decision.trade_candidate.limit_price,
                **quotes,
            }
            prices_used.append(price_row)
            if quotes.get("spread") is not None:
                spread_total += float(quotes["spread"])
        else:
            row = {**decision_row, "execution_reason": execution.reason}
            blocked = (
                decision.status is IntegratedDecisionStatus.BLOCKED
                or str(decision.risk_guard_result).upper() in {"REJECT", "REJECTED", "BLOCKED"}
                or "RISK" in (decision.risk_guard_reason or "").upper()
                or "RISK_GUARD" in (execution.reason or "").upper()
            )
            if blocked:
                why_rejected.append(row)
                risk_blocks += 1
            else:
                why_not.append(row)

        for output in cycle.package.agent_outputs:
            agent_statements.append(
                {
                    "cycle_id": cycle.cycle_id,
                    "agent_name": output.agent_name,
                    "agent_version": output.agent_version,
                    "status": output.status.value if hasattr(output.status, "value") else str(output.status),
                    "findings": list(output.findings),
                    "candidate_action": output.candidate_action.value
                    if hasattr(output.candidate_action, "value")
                    else str(output.candidate_action),
                    "confidence": output.confidence,
                    "evidence": list(output.evidence)[:8],
                }
            )

        for record in cycle.package.dispatch_records:
            if record.execution_time_ms is not None:
                agent_latency.append(
                    {
                        "cycle_id": cycle.cycle_id,
                        "agent_name": record.agent_name,
                        "latency_ms": record.execution_time_ms,
                    }
                )
                decision_latency.append(
                    {
                        "cycle_id": cycle.cycle_id,
                        "agent_name": record.agent_name,
                        "latency_ms": record.execution_time_ms,
                    }
                )

        risk_statements.append(
            {
                "decision_id": decision.decision_id,
                "risk_guard_result": decision.risk_guard_result,
                "risk_guard_reason": decision.risk_guard_reason,
                "status": decision.status.value,
                "approved": decision.status is IntegratedDecisionStatus.CANDIDATE and execution.accepted,
                "rule_results": [
                    {"rule": name, "passed": passed, "detail": detail}
                    for name, passed, detail in decision.risk_rule_results
                ],
            }
        )

    data_available = {
        "snapshot_count": len(snapshots) or len(summary.snapshot_ids),
        "snapshot_ids": list(summary.snapshot_ids),
        "providers": sorted(
            {
                *(snap.provider for snap in snapshots),
                str((summary.provider_health or {}).get("provider") or ""),
            }
            - {""}
        ),
        "market_data_health": summary.market_data_health,
        "option_quote_counts": [
            {
                "snapshot_id": snap.snapshot_id,
                "option_count": len(snap.option_contracts),
                "market_data_source": snap.market_data_source.value
                if hasattr(snap.market_data_source, "value")
                else str(snap.market_data_source),
                "data_quality": snap.data_quality.value,
            }
            for snap in snapshots
        ],
    }

    derived_stale = [dict(row) for row in stale_events]
    derived_failures = [dict(row) for row in data_failures]
    for snap in snapshots:
        if snap.data_quality in {DataQualityStatus.STALE, DataQualityStatus.DEGRADED}:
            derived_stale.append(
                {
                    "snapshot_id": snap.snapshot_id,
                    "data_quality": snap.data_quality.value,
                    "notes": list(snap.quality_notes),
                }
            )
        if snap.data_quality in {DataQualityStatus.INSUFFICIENT, DataQualityStatus.REJECTED}:
            derived_failures.append(
                {
                    "snapshot_id": snap.snapshot_id,
                    "data_quality": snap.data_quality.value,
                    "notes": list(snap.quality_notes),
                }
            )

    if not agent_latency:
        agent_latency = [dict(row) for row in summary.agent_latency]

    if paper is not None:
        for order in paper.ledger.book.fills:
            slip = float(getattr(order, "slippage", 0.0) or 0.0)
            slippage_total += slip
            prices_used.append(
                {
                    "order_id": getattr(order, "paper_order_id", None),
                    "side": getattr(order, "side", None),
                    "price": getattr(order, "price", None),
                    "slippage": slip,
                    "price_source": getattr(order, "price_source", None),
                    "source": "paper_ledger",
                }
            )

    answers = {
        "why_traded": why_traded,
        "why_not_traded": why_not + [dict(row) for row in summary.no_trade_reasons],
        "why_rejected": why_rejected
        + [dict(row) for row in summary.rejected_decisions]
        + [dict(row) for row in summary.risk_guard_rejections],
        "data_available": data_available,
        "agent_statements": agent_statements,
        "risk_guard_statements": risk_statements,
        "prices_used": prices_used,
        "simulated_pnl": {
            "gross_pnl": summary.gross_pnl,
            "charges": summary.charges,
            "net_pnl": summary.net_pnl,
            "max_drawdown": summary.max_drawdown,
            "mtm": dict(summary.mtm),
            "win_loss": dict(summary.win_loss),
        },
    }
    telemetry = {
        "data_failures": derived_failures,
        "reconnects": [dict(row) for row in reconnects],
        "stale_events": derived_stale,
        "decision_latency": decision_latency,
        "agent_latency": agent_latency,
        "execution_simulation": {
            "accepted_count": sum(1 for cycle in cycles if cycle.execution.accepted),
            "cycle_count": len(cycles),
            "monitor_events": [dict(row) for row in monitor_events],
        },
        "slippage": round(slippage_total, 4),
        "spread": round(spread_total, 4),
        "risk_guard_blocks": risk_blocks + len(summary.risk_guard_rejections),
        "decision_count": len(summary.decisions),
        "trade_count": len(summary.trades),
        "rejected_decision_count": len(summary.rejected_decisions),
    }
    return SessionEODReport(
        campaign_id=campaign_id,
        session_id=summary.session_id,
        session_date=session_date,
        evaluation_label=label.value,
        summary=summary.to_dict(),
        answers=answers,
        telemetry=telemetry,
        started_at=summary.started_at,
        ended_at=summary.ended_at,
        status=summary.status,
        profitability_claim=False,
        broker_order_calls=0,
    )


__all__ = [
    "EOD_SCHEMA",
    "SessionEODReport",
    "build_session_eod_report",
]
