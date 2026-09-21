"""HMAC secret for RiskStamp.

Architecture Review #1: there is no published default. Missing or known
secrets fail closed. Tests inject an explicit secret; paper cycles read
GROW_RISK_SECRET from the environment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from grow.errors import GrowConfigError

# Values that have appeared in this tree or are otherwise unusable.
_FORBIDDEN = frozenset(
    {
        "grow-risk-v1-paper-only",
        "changeme",
        "secret",
        "password",
        "test",
        "hmac",
    }
)
_MIN_LEN = 16


def resolve_risk_secret(
    explicit: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    if explicit is not None:
        candidate = str(explicit).strip()
    else:
        env = os.environ if environ is None else environ
        candidate = str(env.get("GROW_RISK_SECRET", "")).strip()
    if not candidate:
        raise GrowConfigError(
            "GROW_RISK_SECRET is required. Risk Guard will not mint stamps "
            "with a published default. Generate one with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if candidate.lower() in _FORBIDDEN:
        raise GrowConfigError("Refusing a known/published HMAC secret.")
    if len(candidate) < _MIN_LEN:
        raise GrowConfigError(f"GROW_RISK_SECRET must be at least {_MIN_LEN} characters.")
    return candidate
