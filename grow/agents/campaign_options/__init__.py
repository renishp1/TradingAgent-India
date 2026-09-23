"""Campaign Options Agent — select allowlisted BUY CE/PE candidates for 4B.

Deterministic. Uses campaign chain filter + DTE bucket scoring (same as
``options.score.v1``). Does not place orders. Risk Guard remains final authority.
"""

from __future__ import annotations

from grow.agents.common import make_result, no_data, require_quality, timed_ms
from grow.config import GrowConfig, load_config
from grow.decision.contracts.agent_result import AgentInput, AgentStatus, CandidateAction
from grow.decision.integration.campaign_score import (
    options_config,
    rank_campaign_quotes,
    score_campaign_quote,
)
from grow.decision.integration.chain_filter import filter_campaign_chain
from grow.market_data.normalized.models import AgentMarketSnapshot, UnderlyingQuoteView
from grow.options.models import OptionType
from grow.options.select import allowed_type
from grow.strategies.indicators import sma


class CampaignOptionsAgent:
    """Produces at most one buyer-only PAPER_OPEN candidate from the shared snapshot."""

    agent_name = "campaign_options"
    agent_version = "campaign_options.v1"

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
        first = next(iter(snapshot.underlyings.values()))
        underlying = first.underlying.strip().upper()
        direction = _direction_from_snapshot(snapshot, first)
        if direction is None:
            return make_result(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                status=AgentStatus.PASS,
                observations=(f"underlying={underlying}", "direction=NEUTRAL"),
                findings=("NO_DIRECTION",),
                interpretation=("No bullish/bearish structure; no campaign PAPER_OPEN.",),
                assumptions=("Buyer-only. Risk Guard limits are not modified.",),
                evidence=(f"snapshot_id={snapshot.snapshot_id}",),
                candidate_action=CandidateAction.NONE,
                invalidation_reason="neutral technical structure",
                cycle_id=cycle_id,
                confidence=0.2,
            )

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
            return make_result(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                status=AgentStatus.PASS,
                observations=(
                    f"underlying={underlying}",
                    f"direction={direction}",
                    f"filter={reason}",
                ),
                findings=("CHAIN_FILTER_NO_CANDIDATE", reason),
                calculated_metrics={
                    "direction": direction,
                    "underlying": underlying,
                    "filter_reason": reason,
                },
                interpretation=("Campaign chain filter produced no eligible instrument.",),
                assumptions=("Buyer-only. Never invent strikes or expiries.",),
                evidence=(f"snapshot_id={snapshot.snapshot_id}",),
                candidate_action=CandidateAction.NONE,
                invalidation_reason=reason,
                cycle_id=cycle_id,
                confidence=0.3,
            )

        spot = float(filtered.diagnostics.get("spot") or first.spot or first.ltp or 0.0)
        atm = float(filtered.atm or 0.0)
        if spot <= 0 or atm <= 0:
            # Fall back: compute from first eligible.
            spot = float(first.spot or first.ltp or filtered.eligible_quotes[0].strike)
            atm = float(filtered.atm or filtered.eligible_quotes[0].strike)

        ranked = rank_campaign_quotes(
            filtered.eligible_quotes,
            spot=spot,
            atm=atm,
            as_of=snapshot.decision_timestamp,
            option_type=opt_type,
            config=options_config(cfg),
        )
        if not ranked:
            return make_result(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                status=AgentStatus.PASS,
                findings=("SCORE_EMPTY",),
                candidate_action=CandidateAction.NONE,
                invalidation_reason="no scored quotes",
                cycle_id=cycle_id,
                confidence=0.2,
            )

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
            return make_result(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                status=AgentStatus.PASS,
                findings=("NO_PREMIUM",),
                candidate_action=CandidateAction.NONE,
                invalidation_reason="NO_PREMIUM",
                cycle_id=cycle_id,
                confidence=0.2,
            )
        # Planned stop / target band for RiskGuard per-trade risk (premium-based).
        stop = round(max(ask * 0.80, 0.05), 2)
        target = round(ask * 1.20, 2)
        lot_size = int(winner.lot_size or 1)
        quantity = lot_size
        instrument = (
            winner.provider_contract_id
            or f"{winner.underlying}-{winner.strike:g}-{winner.option_type}"
        )
        return make_result(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
            status=AgentStatus.PASS,
            observations=(
                f"underlying={underlying}",
                f"direction={direction}",
                f"instrument={instrument}",
                f"score={total:.4f}",
                f"dte={score_diag.get('dte_days')}",
            ),
            calculated_metrics={
                "strategy": "campaign_options",
                "direction": direction,
                "underlying": underlying,
                "limit_price": ask,
                "stop_loss": stop,
                "target": target,
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
            },
            interpretation=(
                f"Selected BUY {winner.option_type} via campaign chain filter + DTE score.",
                "Theta greek is not part of the numeric score (calendar DTE only).",
            ),
            findings=("CAMPAIGN_OPTION_CANDIDATE", "BUYER_ONLY"),
            assumptions=(
                "Buyer-only options. Risk Guard limits are not modified by agent outputs.",
                "CANDIDATE is a paper-trade candidate and is not a live or broker order.",
            ),
            evidence=(
                f"snapshot_id={snapshot.snapshot_id}",
                f"instrument={instrument}",
                f"score_version={score_diag.get('score_version')}",
            ),
            metrics_used=(
                "direction",
                "limit_price",
                "stop_loss",
                "quantity",
                "option_type",
                "strike",
                "expiry",
                "score",
                "dte_days",
            ),
            candidate_action=CandidateAction.PAPER_OPEN,
            candidate_instrument=instrument,
            entry_reason=f"campaign_options score={total:.4f} dte={score_diag.get('dte_days')}",
            invalidation_reason=None,
            risk_flags=(),
            cycle_id=cycle_id,
            confidence=min(0.75, max(0.35, float(total))),
        )


def _direction_from_snapshot(
    snapshot: AgentMarketSnapshot,
    quote: UnderlyingQuoteView,
) -> str | None:
    """Derive BULLISH/BEARISH from snapshot history; never invent bars."""
    raw = snapshot.diagnostics.get("history_closes") if snapshot.diagnostics else None
    closes: list[float] = []
    if isinstance(raw, (list, tuple)) and raw:
        closes = [float(v) for v in raw]
    else:
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
    types = {row.option_type.upper() for row in snapshot.option_contracts}
    if types == {"CE"}:
        return "BULLISH"
    if types == {"PE"}:
        return "BEARISH"
    return None


__all__ = ["CampaignOptionsAgent"]
