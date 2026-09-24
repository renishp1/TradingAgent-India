"""Campaign Options Agent — select allowlisted BUY CE/PE candidates for 4B.

Deterministic. Uses campaign chain filter + DTE bucket scoring (same as
``options.score.v1``). Does not place orders. Risk Guard remains final authority.

Before PAPER_OPEN, candidates must satisfy the same cash / per-trade risk
formulas RiskGuard enforces (early feasibility filter — not a bypass).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from grow.agents.common import make_result, no_data, require_quality, timed_ms
from grow.config import GrowConfig, load_config
from grow.decision.contracts.agent_result import AgentInput, AgentStatus, CandidateAction
from grow.decision.integration.campaign_score import (
    options_config,
    rank_campaign_quotes,
    score_campaign_quote,
)
from grow.decision.integration.chain_filter import filter_campaign_chain
from grow.market_data.normalized.models import AgentMarketSnapshot, OptionQuoteView, UnderlyingQuoteView
from grow.options.select import allowed_type
from grow.strategies.indicators import sma

# Same premium stop / target band used when emitting the candidate for RiskGuard.
_STOP_FRACTION = 0.80
_TARGET_FRACTION = 1.20
_MIN_STOP = 0.05
_DEFAULT_LOTS = 1

NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE = "NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE"


class CampaignOptionsAgent:
    """Produces at most one buyer-only PAPER_OPEN candidate from the shared snapshot."""

    agent_name = "campaign_options"
    agent_version = "campaign_options.v3"

    def __init__(self, config: GrowConfig | None = None) -> None:
        self.config = config

    def analyze(self, snapshot: AgentMarketSnapshot, *, cycle_id: str = ""):
        return timed_ms(lambda: self._analyze(snapshot, cycle_id=cycle_id))

    def analyze_input(self, agent_input: AgentInput):
        return self.analyze(agent_input.snapshot, cycle_id=agent_input.cycle_id)

    def _analyze(self, snapshot: AgentMarketSnapshot, *, cycle_id: str = ""):
        blocked = require_quality(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
            cycle_id=cycle_id,
        )
        if blocked is not None:
            return blocked
        if not snapshot.option_contracts:
            return no_data(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=("option_contracts",),
                observations=("option chain absent; cannot select campaign candidate",),
                cycle_id=cycle_id,
            )
        if not snapshot.underlyings:
            return no_data(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=("underlyings",),
                cycle_id=cycle_id,
            )

        cfg = self.config or load_config()
        configured = tuple(str(s).strip().upper() for s in cfg.strategies.universe)
        present = tuple(
            str(k).strip().upper()
            for k in snapshot.underlyings.keys()
            if str(k).strip().upper() in set(configured) or not configured
        )
        # Prefer configured order; fall back to snapshot keys so fixtures still work.
        underlyings = present if present else tuple(str(k).strip().upper() for k in snapshot.underlyings.keys())
        if not underlyings:
            return no_data(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=("underlyings",),
                cycle_id=cycle_id,
            )

        available_cash, max_trade_risk, budget_reason = _paper_budget(cfg)
        if budget_reason is not None:
            return make_result(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                status=AgentStatus.PASS,
                observations=(f"feasibility={budget_reason}",),
                findings=(NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE, budget_reason),
                calculated_metrics={
                    "feasibility_reason": budget_reason,
                    "candidates_checked": 0,
                    "candidates_rejected_cash": 0,
                    "candidates_rejected_risk": 0,
                    "candidates_rejected_incomplete": 0,
                    "available_cash": available_cash,
                    "max_per_trade_risk": max_trade_risk,
                    "underlyings_evaluated": list(underlyings),
                },
                interpretation=("Paper cash/risk configuration incomplete; fail closed.",),
                assumptions=("Buyer-only. Risk Guard remains final authority.",),
                evidence=(f"snapshot_id={snapshot.snapshot_id}",),
                candidate_action=CandidateAction.NONE,
                invalidation_reason=NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE,
                cycle_id=cycle_id,
                confidence=0.2,
            )

        per_index: dict[str, Any] = {}
        scored_winners: list[tuple[float, Any, dict[str, Any], dict[str, Any]]] = []
        agg_checked = agg_cash = agg_risk = agg_incomplete = 0
        cash_needs: list[float] = []
        risk_needs: list[float] = []
        any_direction = False

        for underlying in underlyings:
            quote = snapshot.underlyings.get(underlying) or snapshot.underlyings.get(underlying.title())
            if quote is None:
                # case-insensitive fallback
                quote = next(
                    (v for k, v in snapshot.underlyings.items() if str(k).upper() == underlying),
                    None,
                )
            if quote is None:
                per_index[underlying] = {"status": "MISSING_UNDERLYING"}
                continue
            direction = _direction_from_snapshot(snapshot, quote, underlying=underlying)
            if direction is None:
                per_index[underlying] = {"status": "NO_DIRECTION"}
                continue
            any_direction = True
            opt_type = allowed_type(direction)
            assert opt_type is not None
            filtered = filter_campaign_chain(
                snapshot,
                underlying=underlying,
                direction=direction,
                config=cfg,
            )
            if filtered.reason_codes or not filtered.eligible_quotes:
                reason = filtered.reason_codes[0] if filtered.reason_codes else "CHAIN_UNIVERSE_EMPTY"
                per_index[underlying] = {"status": "CHAIN_FILTER", "reason": reason, "direction": direction}
                continue
            spot = float(filtered.diagnostics.get("spot") or quote.spot or quote.ltp or 0.0)
            atm = float(filtered.atm or 0.0)
            if spot <= 0 or atm <= 0:
                spot = float(quote.spot or quote.ltp or filtered.eligible_quotes[0].strike)
                atm = float(filtered.atm or filtered.eligible_quotes[0].strike)
            feasible, feasibility = _feasible_quotes(
                filtered.eligible_quotes,
                lots=_DEFAULT_LOTS,
                available_cash=float(available_cash),
                max_per_trade_risk=float(max_trade_risk),
            )
            agg_checked += int(feasibility["candidates_checked"])
            agg_cash += int(feasibility["candidates_rejected_cash"])
            agg_risk += int(feasibility["candidates_rejected_risk"])
            agg_incomplete += int(feasibility["candidates_rejected_incomplete"])
            if feasibility.get("minimum_cash_required") is not None:
                cash_needs.append(float(feasibility["minimum_cash_required"]))
            if feasibility.get("minimum_planned_risk") is not None:
                risk_needs.append(float(feasibility["minimum_planned_risk"]))
            if not feasible:
                per_index[underlying] = {
                    "status": NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE,
                    "direction": direction,
                    **feasibility,
                }
                continue
            ranked = rank_campaign_quotes(
                feasible,
                spot=spot,
                atm=atm,
                as_of=snapshot.decision_timestamp,
                option_type=opt_type,
                config=options_config(cfg),
            )
            if not ranked:
                per_index[underlying] = {"status": "SCORE_EMPTY", "direction": direction}
                continue
            winner = ranked[0]
            total, score_diag = score_campaign_quote(
                winner,
                spot=spot,
                atm=atm,
                as_of=snapshot.decision_timestamp,
                option_type=opt_type,
                config=options_config(cfg),
            )
            ask = float(winner.ask or winner.ltp or 0.0)
            if ask <= 0:
                per_index[underlying] = {"status": "NO_PREMIUM", "direction": direction}
                continue
            stop = premium_stop_loss(ask)
            target = premium_take_profit(ask)
            lots = _DEFAULT_LOTS
            lot_size = int(winner.lot_size or 0)
            quantity = lots * lot_size
            instrument = (
                winner.provider_contract_id
                or f"{winner.underlying}-{winner.strike:g}-{winner.option_type}"
            )
            payload = {
                "strategy": "campaign_options",
                "direction": direction,
                "underlying": underlying,
                "limit_price": ask,
                "stop_loss": stop,
                "target": target,
                "lots": lots,
                "quantity": quantity,
                "lot_size": lot_size,
                "option_type": winner.option_type,
                "strike": float(winner.strike),
                "expiry": winner.expiry.isoformat(),
                "score": total,
                "dte_days": score_diag.get("dte_days"),
                "time_to_expiry_score": score_diag.get("time_to_expiry"),
                "theta": winner.theta,
                "theta_in_score": False,
                "selected_expiry": filtered.selected_expiry,
                "atm": atm,
                "required_cash": round(ask * quantity, 2),
                "planned_risk": round(abs(ask - stop) * quantity, 2),
                "instrument": instrument,
                "score_version": score_diag.get("score_version"),
            }
            per_index[underlying] = {"status": "CANDIDATE", **payload, **feasibility}
            scored_winners.append((float(total), winner, payload, feasibility))

        feasibility_summary = {
            "candidates_checked": agg_checked,
            "candidates_rejected_cash": agg_cash,
            "candidates_rejected_risk": agg_risk,
            "candidates_rejected_incomplete": agg_incomplete,
            "minimum_cash_required": min(cash_needs) if cash_needs else None,
            "minimum_planned_risk": min(risk_needs) if risk_needs else None,
            "available_cash": available_cash,
            "max_per_trade_risk": max_trade_risk,
            "underlyings_evaluated": list(underlyings),
            "per_index": per_index,
        }

        if not any_direction:
            return make_result(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                status=AgentStatus.PASS,
                observations=(f"underlyings={','.join(underlyings)}", "direction=NEUTRAL"),
                findings=("NO_DIRECTION",),
                calculated_metrics=feasibility_summary,
                interpretation=("No bullish/bearish structure on configured underlyings.",),
                assumptions=("Buyer-only. Risk Guard limits are not modified.",),
                evidence=(f"snapshot_id={snapshot.snapshot_id}",),
                candidate_action=CandidateAction.NONE,
                invalidation_reason="neutral technical structure",
                cycle_id=cycle_id,
                confidence=0.2,
            )

        if not scored_winners:
            reason = NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE
            if all(str(v.get("status")) == "CHAIN_FILTER" for v in per_index.values()):
                reason = "CHAIN_FILTER_NO_CANDIDATE"
            # Preserve a summary direction for single-index / diagnostic consumers.
            summary_direction = next(
                (
                    str(v["direction"])
                    for v in per_index.values()
                    if isinstance(v, dict) and v.get("direction")
                ),
                None,
            )
            return make_result(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                status=AgentStatus.PASS,
                observations=(
                    f"underlyings={','.join(underlyings)}",
                    f"feasibility={reason}",
                    f"checked={agg_checked}",
                    *(() if summary_direction is None else (f"direction={summary_direction}",)),
                ),
                findings=(reason,),
                calculated_metrics={
                    "feasibility_reason": reason,
                    "direction": summary_direction,
                    **feasibility_summary,
                },
                interpretation=("No campaign option satisfied chain/feasibility gates.",),
                assumptions=(
                    "Buyer-only. Feasibility uses the same cash/risk formulas as RiskGuard.",
                    "Risk Guard remains final authority.",
                ),
                evidence=(f"snapshot_id={snapshot.snapshot_id}",),
                candidate_action=CandidateAction.NONE,
                invalidation_reason=reason,
                cycle_id=cycle_id,
                confidence=0.35,
            )

        scored_winners.sort(key=lambda item: (-item[0], item[2]["underlying"], item[2]["instrument"]))
        _total, winner, payload, _feas = scored_winners[0]
        instrument = payload["instrument"]
        return make_result(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
            status=AgentStatus.PASS,
            observations=(
                f"underlying={payload['underlying']}",
                f"direction={payload['direction']}",
                f"instrument={instrument}",
                f"score={payload['score']:.4f}",
                f"dte={payload.get('dte_days')}",
                f"underlyings_evaluated={','.join(underlyings)}",
            ),
            calculated_metrics={
                **payload,
                **feasibility_summary,
                "available_cash": available_cash,
                "max_per_trade_risk": max_trade_risk,
            },
            interpretation=(
                f"Selected BUY {winner.option_type} via per-index chain filter + DTE score.",
                "Theta greek is not part of the numeric score (calendar DTE only).",
                "Candidate passed early cash/risk feasibility; Risk Guard remains final.",
            ),
            findings=("CAMPAIGN_OPTION_CANDIDATE", "BUYER_ONLY"),
            assumptions=(
                "Buyer-only options. Risk Guard limits are not modified by agent outputs.",
                "CANDIDATE is a paper-trade candidate and is not a live or broker order.",
            ),
            evidence=(
                f"snapshot_id={snapshot.snapshot_id}",
                f"instrument={instrument}",
                f"score_version={payload.get('score_version')}",
            ),
            metrics_used=(
                "direction",
                "limit_price",
                "stop_loss",
                "lots",
                "quantity",
                "lot_size",
                "option_type",
                "strike",
                "expiry",
                "score",
                "dte_days",
            ),
            candidate_action=CandidateAction.PAPER_OPEN,
            candidate_instrument=instrument,
            entry_reason=f"campaign_options score={payload['score']:.4f} dte={payload.get('dte_days')}",
            invalidation_reason=None,
            risk_flags=(),
            cycle_id=cycle_id,
            confidence=min(0.75, max(0.35, float(payload["score"]))),
        )


def premium_stop_loss(ask: float) -> float:
    """Premium-based stop used for campaign buyers (matches RiskGuard planned-risk path)."""
    return round(max(float(ask) * _STOP_FRACTION, _MIN_STOP), 2)


def premium_take_profit(ask: float) -> float:
    return round(float(ask) * _TARGET_FRACTION, 2)


def _paper_budget(cfg: GrowConfig) -> tuple[float | None, float | None, str | None]:
    cash = cfg.paper.starting_cash
    risk_cap = cfg.risk.max_per_trade_risk
    if cash is None or float(cash) <= 0:
        return None, None if risk_cap is None else float(risk_cap), "MISSING_AVAILABLE_CASH"
    if risk_cap is None or float(risk_cap) <= 0:
        return float(cash), None, "MISSING_MAX_PER_TRADE_RISK"
    return float(cash), float(risk_cap), None


@dataclass(frozen=True)
class _FeasibilityRow:
    quote: OptionQuoteView
    ask: float
    lot_size: int
    quantity: int
    stop_loss: float
    required_cash: float
    planned_risk: float
    ok: bool
    reject_reason: str | None


def evaluate_campaign_feasibility(
    quote: OptionQuoteView,
    *,
    lots: int,
    available_cash: float,
    max_per_trade_risk: float,
) -> _FeasibilityRow:
    """Same formulas as RiskGuard cash.available + risk.per_trade for a buyer open."""
    ask_raw = quote.ask if quote.ask is not None else quote.ltp
    if ask_raw is None or float(ask_raw) <= 0:
        return _FeasibilityRow(
            quote=quote,
            ask=0.0,
            lot_size=0,
            quantity=0,
            stop_loss=0.0,
            required_cash=0.0,
            planned_risk=0.0,
            ok=False,
            reject_reason="MISSING_ASK",
        )
    if quote.lot_size is None or int(quote.lot_size) < 1:
        return _FeasibilityRow(
            quote=quote,
            ask=float(ask_raw),
            lot_size=0,
            quantity=0,
            stop_loss=0.0,
            required_cash=0.0,
            planned_risk=0.0,
            ok=False,
            reject_reason="MISSING_LOT_SIZE",
        )
    if lots < 1:
        return _FeasibilityRow(
            quote=quote,
            ask=float(ask_raw),
            lot_size=int(quote.lot_size),
            quantity=0,
            stop_loss=0.0,
            required_cash=0.0,
            planned_risk=0.0,
            ok=False,
            reject_reason="MISSING_LOTS",
        )
    ask = float(ask_raw)
    lot_size = int(quote.lot_size)
    quantity = lots * lot_size
    stop = premium_stop_loss(ask)
    required_cash = round(ask * quantity, 2)
    planned_risk = round(abs(ask - stop) * quantity, 2)
    if required_cash > float(available_cash) + 1e-9:
        return _FeasibilityRow(
            quote=quote,
            ask=ask,
            lot_size=lot_size,
            quantity=quantity,
            stop_loss=stop,
            required_cash=required_cash,
            planned_risk=planned_risk,
            ok=False,
            reject_reason="CASH",
        )
    if planned_risk > float(max_per_trade_risk) + 1e-9:
        return _FeasibilityRow(
            quote=quote,
            ask=ask,
            lot_size=lot_size,
            quantity=quantity,
            stop_loss=stop,
            required_cash=required_cash,
            planned_risk=planned_risk,
            ok=False,
            reject_reason="RISK",
        )
    return _FeasibilityRow(
        quote=quote,
        ask=ask,
        lot_size=lot_size,
        quantity=quantity,
        stop_loss=stop,
        required_cash=required_cash,
        planned_risk=planned_risk,
        ok=True,
        reject_reason=None,
    )


def _feasible_quotes(
    quotes: tuple[OptionQuoteView, ...],
    *,
    lots: int,
    available_cash: float,
    max_per_trade_risk: float,
) -> tuple[tuple[OptionQuoteView, ...], dict[str, Any]]:
    checked = 0
    rejected_cash = 0
    rejected_risk = 0
    rejected_incomplete = 0
    cash_needs: list[float] = []
    risk_needs: list[float] = []
    ok_rows: list[OptionQuoteView] = []
    for quote in quotes:
        checked += 1
        row = evaluate_campaign_feasibility(
            quote,
            lots=lots,
            available_cash=available_cash,
            max_per_trade_risk=max_per_trade_risk,
        )
        if row.required_cash > 0:
            cash_needs.append(row.required_cash)
        if row.planned_risk > 0:
            risk_needs.append(row.planned_risk)
        if row.ok:
            ok_rows.append(quote)
            continue
        if row.reject_reason == "CASH":
            rejected_cash += 1
        elif row.reject_reason == "RISK":
            rejected_risk += 1
        else:
            rejected_incomplete += 1
    diagnostics = {
        "candidates_checked": checked,
        "candidates_rejected_cash": rejected_cash,
        "candidates_rejected_risk": rejected_risk,
        "candidates_rejected_incomplete": rejected_incomplete,
        "minimum_cash_required": min(cash_needs) if cash_needs else None,
        "minimum_planned_risk": min(risk_needs) if risk_needs else None,
        "available_cash": available_cash,
        "max_per_trade_risk": max_per_trade_risk,
    }
    return tuple(ok_rows), diagnostics


def _direction_from_snapshot(
    snapshot: AgentMarketSnapshot,
    quote: UnderlyingQuoteView,
    *,
    underlying: str | None = None,
) -> str | None:
    """Derive BULLISH/BEARISH from snapshot history; never invent bars."""
    name = (underlying or quote.underlying or "").strip().upper()
    closes: list[float] = []
    by = snapshot.diagnostics.get("history_closes_by_underlying") if snapshot.diagnostics else None
    if isinstance(by, Mapping) and name and name in by and by[name]:
        closes = [float(v) for v in by[name]]
    else:
        raw = snapshot.diagnostics.get("history_closes") if snapshot.diagnostics else None
        if isinstance(raw, (list, tuple)) and raw:
            # Only use shared history_closes when this is the sole underlying.
            if len(snapshot.underlyings) == 1:
                closes = [float(v) for v in raw]
    if not closes:
        for value in (quote.open, quote.high, quote.low, quote.close, quote.ltp or quote.spot):
            if value is not None:
                closes.append(float(value))
    if len(closes) >= 14:
        fast = sma(tuple(closes), 5)
        slow = sma(tuple(closes), 14)
        if fast is not None and slow is not None:
            if fast > slow:
                return "BULLISH"
            if fast < slow:
                return "BEARISH"
    # Sparse snapshot fallback: prefer CE when a CE is present with OK quality and spot.
    types = {
        row.option_type.upper()
        for row in snapshot.option_contracts
        if not name or row.underlying.upper() == name
    }
    if types == {"CE"}:
        return "BULLISH"
    if types == {"PE"}:
        return "BEARISH"
    return None


__all__ = [
    "CampaignOptionsAgent",
    "NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE",
    "evaluate_campaign_feasibility",
    "premium_stop_loss",
    "premium_take_profit",
]
