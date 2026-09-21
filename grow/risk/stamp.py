"""Canonical RiskStamp payload.

The stamp must represent *this exact proposal*, not a subset of identity
fields. Review #1 follow-up: bind security-relevant TradeProposal fields
via canonical JSON → SHA-256 → HMAC-SHA256.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from grow.types import TradeProposal

STAMP_VERSION = "grow.risk.stamp.v2"


def _money(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{float(value):.4f}"


def canonical_proposal_json(proposal: TradeProposal, ruleset: str) -> str:
    """Deterministic JSON of every field that changes what would fill."""
    body = {
        "exchange": proposal.symbol.exchange,
        "intent": proposal.intent.value,
        "limit_price": _money(proposal.limit_price),
        "notional": _money(proposal.notional),
        "proposal_id": proposal.proposal_id,
        "quantity": int(proposal.quantity),
        "ruleset": ruleset,
        "side": proposal.side.value,
        "stop_loss": _money(proposal.stop_loss),
        "symbol": proposal.symbol.ticker,
        "take_profit": _money(proposal.take_profit),
        "v": STAMP_VERSION,
        "venue": proposal.venue.value,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stamp_token(proposal: TradeProposal, ruleset: str, secret: str) -> str:
    payload = canonical_proposal_json(proposal, ruleset).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return hmac.new(secret.encode("utf-8"), digest, hashlib.sha256).hexdigest()
