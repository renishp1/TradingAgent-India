"""Build an immutable ResearchPacket. No raw OHLCV. No option-chain arrays."""

from __future__ import annotations

from grow.clock import IST
from grow.data.boundary import assert_research_payload
from grow.data.schema import ResearchView
from grow.options.models import OptionCandidate, OptionsDecision
from grow.research.models import PACKET_SCHEMA, ResearchPacket, packet_id_for
from grow.strategies.signal import StrategySignal


def _ist(moment) -> None:
    if moment.tzinfo is None or getattr(moment.tzinfo, "key", None) != "Asia/Kolkata":
        raise ValueError("packet timestamps must be Asia/Kolkata")


def candidate_summary(candidate: OptionCandidate) -> dict:
    """LLM-safe candidate view. `volume` renamed so OHLCV keys never leak."""
    return {
        "candidate_id": candidate.candidate_id,
        "underlying": candidate.underlying,
        "direction": candidate.direction,
        "option_type": candidate.option_type,
        "intent": candidate.intent,
        "expiry": candidate.expiry.isoformat(),
        "strike": candidate.strike,
        "contract_symbol": candidate.contract_symbol,
        "spot_price": candidate.spot_price,
        "premium_reference": candidate.premium_reference,
        "bid": candidate.bid,
        "ask": candidate.ask,
        "spread_pct": candidate.spread_pct,
        "open_interest": candidate.open_interest,
        "option_volume": candidate.volume,
        "implied_volatility": candidate.implied_volatility,
        "delta": candidate.delta,
        "moneyness": candidate.moneyness,
        "score_total": candidate.score.total,
        "score_version": candidate.score.version,
        "as_of": candidate.as_of.isoformat(),
        "underlying_snapshot_id": candidate.underlying_snapshot_id,
        "option_chain_snapshot_id": candidate.option_chain_snapshot_id,
        "strategy_signal_id": candidate.strategy_signal_id,
    }


def build_packet(
    *,
    view: ResearchView,
    signal: StrategySignal,
    options: OptionsDecision,
    configuration_version: str,
) -> ResearchPacket:
    _ist(view.as_of)
    _ist(signal.as_of)
    _ist(options.as_of)
    if not (view.as_of == signal.as_of == options.as_of):
        raise ValueError("ASOF_MISMATCH")
    candidate = options.candidate
    summary = None if candidate is None else candidate_summary(candidate)
    rejected = tuple(f"{item.identity[3]} {item.identity[2]}:{item.reason}" for item in options.rejected[:12])
    snapshot_ids = {
        "market": view.snapshot_id,
        "signal": signal.snapshot_id,
        "options_underlying": options.underlying_snapshot_id,
        "options_chain": options.option_chain_snapshot_id,
    }
    pid = packet_id_for(
        {
            "candidate": None if candidate is None else candidate.candidate_id,
            "chain": options.option_chain_snapshot_id,
            "schema": PACKET_SCHEMA,
            "signal": signal.signal_id,
            "snapshot": view.snapshot_id,
            "status": options.status.value,
        }
    )
    packet = ResearchPacket(
        packet_id=pid,
        as_of=view.as_of,
        underlying=view.symbol.ticker,
        strategy_signal_id=signal.signal_id,
        strategy_signal_version=signal.strategy_version,
        options_decision_id=options.option_chain_snapshot_id,
        option_candidate_id=None if candidate is None else candidate.candidate_id,
        market_research_view=view.to_dict(),
        strategy_evidence=signal.to_dict(),
        option_candidate=summary,
        rejected_candidate_summary=rejected,
        session_context={
            "session": view.session.value,
            "quality_stale": view.quality_stale,
            "quality_complete": view.quality_complete,
            "missing_count": view.missing_count,
            "timezone": IST.key,
        },
        configuration_version=configuration_version,
        data_snapshot_ids=snapshot_ids,
        packet_schema_version=PACKET_SCHEMA,
        options_status=options.status.value,
        strategy_direction=signal.direction,
        evidence_for_agents=(
            "market_research_view",
            "strategy_evidence",
            "option_candidate",
            "rejected_candidate_summary",
            "session_context",
        ),
    )
    assert_research_payload(packet.to_dict())
    return packet
