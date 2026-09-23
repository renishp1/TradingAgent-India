"""Deterministic campaign CEO gate — research approval, never execution.

Mirrors the fixture ``CEOAgent`` rules used on LivePaperLoop (bull→CE, bear→PE,
candidate required) without calling an LLM and without placing orders.
Applied on the campaign DecisionIntegrator path after policy selection and
before Risk Guard. Cannot raise risk limits or stamp proposals.
"""

from __future__ import annotations

from grow.decision.integration.contract import StrategyCandidate


def campaign_ceo_gate(candidate: StrategyCandidate) -> tuple[bool, tuple[str, ...]]:
    """Return (approved, rejection_reasons). Empty reasons when approved."""
    reasons: list[str] = []
    direction = candidate.direction
    option_type = (candidate.option_type or "").upper()
    if direction == "BULLISH" and option_type != "CE":
        reasons.append("BULLISH_NOT_CE")
    if direction == "BEARISH" and option_type != "PE":
        reasons.append("BEARISH_NOT_PE")
    if direction not in {"BULLISH", "BEARISH"}:
        reasons.append(f"INVALID_DIRECTION:{direction}")
    if option_type not in {"CE", "PE"}:
        reasons.append("OPTIONS_NO_TRADE")
    if candidate.strike is None or candidate.strike <= 0 or not candidate.expiry:
        reasons.append("INCOMPLETE_OPTION_CANDIDATE")
    if candidate.quantity < 1 or candidate.limit_price <= 0 or candidate.stop_loss <= 0:
        reasons.append("INCOMPLETE_CANDIDATE")
    if reasons:
        return False, tuple(dict.fromkeys(reasons))
    return True, ()


__all__ = ["campaign_ceo_gate"]
