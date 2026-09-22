"""Deterministic market-data health for paper-trading gates.

SessionHealth remains the provider/loop transport state machine.
MarketDataHealth is the Phase-3 authority for whether *new* paper entries
are allowed. Only HEALTHY permits new entries. Existing positions may still
mark-to-market from the last valid quote without fabricating prices.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping

from grow.live_data.models import SessionHealth

MARKET_DATA_NOT_HEALTHY = "MARKET_DATA_NOT_HEALTHY"

# Quality status values mirrored as strings so this module never imports
# grow.market_data (avoids circular import via snapshots.builder → health).
_QUALITY_OK = "OK"
_QUALITY_STALE = "STALE"
_QUALITY_DEGRADED = "DEGRADED"
_QUALITY_INSUFFICIENT = "INSUFFICIENT"
_QUALITY_REJECTED = "REJECTED"


class MarketDataHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    DISCONNECTED = "DISCONNECTED"
    FAILED = "FAILED"


_SESSION_TO_MARKET: dict[SessionHealth, MarketDataHealth] = {
    SessionHealth.READY: MarketDataHealth.HEALTHY,
    SessionHealth.RUNNING: MarketDataHealth.HEALTHY,
    SessionHealth.STALE: MarketDataHealth.STALE,
    SessionHealth.DEGRADED: MarketDataHealth.DEGRADED,
    SessionHealth.DISCONNECTED: MarketDataHealth.DISCONNECTED,
    SessionHealth.CONNECTING: MarketDataHealth.DISCONNECTED,
    SessionHealth.STOPPED: MarketDataHealth.FAILED,
}


def map_session_health(state: SessionHealth | str) -> MarketDataHealth:
    """Map transport SessionHealth onto the Phase-3 MarketDataHealth set."""
    if isinstance(state, str):
        try:
            state = SessionHealth(state)
        except ValueError:
            return MarketDataHealth.FAILED
    return _SESSION_TO_MARKET.get(state, MarketDataHealth.FAILED)


def market_data_health_from_quality(quality: Any) -> MarketDataHealth:
    """Derive feed health from an agent snapshot quality gate when no session state is present.

    Accepts DataQualityStatus or its string value without importing market_data.
    """
    value = getattr(quality, "value", quality)
    if not isinstance(value, str):
        return MarketDataHealth.FAILED
    if value == _QUALITY_OK:
        return MarketDataHealth.HEALTHY
    if value == _QUALITY_STALE:
        return MarketDataHealth.STALE
    if value == _QUALITY_DEGRADED:
        return MarketDataHealth.DEGRADED
    if value == _QUALITY_INSUFFICIENT:
        return MarketDataHealth.DEGRADED
    if value == _QUALITY_REJECTED:
        return MarketDataHealth.FAILED
    return MarketDataHealth.FAILED


def resolve_market_data_health(
    *,
    session_state: SessionHealth | str | None = None,
    data_quality: Any | None = None,
    diagnostics: Mapping[str, Any] | None = None,
    freshness_ok: bool | None = None,
) -> MarketDataHealth:
    """Resolve the authoritative MarketDataHealth for a cycle.

    Precedence:
    1. Explicit diagnostics.market_data_health when valid
    2. SessionHealth mapping (transport / loop)
    3. Snapshot data quality
    4. Freshness flag
    """
    diag = dict(diagnostics or {})
    raw = diag.get("market_data_health")
    if raw in {item.value for item in MarketDataHealth}:
        health = MarketDataHealth(str(raw))
    elif session_state is not None:
        health = map_session_health(session_state)
    elif data_quality is not None:
        health = market_data_health_from_quality(data_quality)
    else:
        health = MarketDataHealth.DISCONNECTED

    if freshness_ok is False and health is MarketDataHealth.HEALTHY:
        return MarketDataHealth.STALE
    if diag.get("provider_failed") is True and health is MarketDataHealth.HEALTHY:
        return MarketDataHealth.FAILED
    return health


def allows_new_paper_trade(health: MarketDataHealth | str) -> bool:
    """New paper entries require HEALTHY market data. Never fabricate a green light."""
    if isinstance(health, str):
        try:
            health = MarketDataHealth(health)
        except ValueError:
            return False
    return health is MarketDataHealth.HEALTHY


def reject_unhealthy_market_data(
    *,
    session_state: SessionHealth | str | None = None,
    data_quality: Any | None = None,
    diagnostics: Mapping[str, Any] | None = None,
    freshness_ok: bool | None = None,
) -> str | None:
    """Return MARKET_DATA_NOT_HEALTHY when new paper trades must be refused."""
    health = resolve_market_data_health(
        session_state=session_state,
        data_quality=data_quality,
        diagnostics=diagnostics,
        freshness_ok=freshness_ok,
    )
    if allows_new_paper_trade(health):
        return None
    return MARKET_DATA_NOT_HEALTHY
