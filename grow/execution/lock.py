"""Paper-trading-only safety lock.

Three independent layers, all fail-closed:

1. Compile-time constant ``LIVE_TRADING_COMPILED = False``.
2. Config / env parser rejects any mode other than ``paper``.
3. Runtime asserts before any fill.

There is no code path that sets LIVE_TRADING_COMPILED to True in milestone 1.
Environment variables cannot override the constant. Broker SDKs are not
imported anywhere in this tree.
"""

from __future__ import annotations

import os
from typing import Iterable

from grow.errors import GrowLiveTradingDisabled

LIVE_TRADING_COMPILED = False
ALLOWED_EXECUTION_MODES = frozenset({"paper"})

_LIVE_TRUTH = frozenset({"1", "true", "yes", "on", "live", "enabled"})
_FORBIDDEN_ENV = (
    "GROW_LIVE_TRADING",
    "LIVE_TRADING_ENABLED",
    "GROW_BROKER",
    "BROKER_API_KEY",
    "KITE_ACCESS_TOKEN",
    "UPSTOX_ACCESS_TOKEN",
    "DHAN_ACCESS_TOKEN",
)


def assert_paper_compiled() -> None:
    if LIVE_TRADING_COMPILED:
        raise GrowLiveTradingDisabled(
            "LIVE_TRADING_COMPILED is True. This binary is unsafe and must not run."
        )


def normalize_execution_mode(mode: str | None) -> str:
    value = (mode or "paper").strip().lower()
    if value not in ALLOWED_EXECUTION_MODES:
        raise GrowLiveTradingDisabled(
            f"Execution mode {mode!r} is forbidden. "
            "Grow milestone 1 is paper-trading only."
        )
    return "paper"


def inspect_environment(environ: Iterable[tuple[str, str]] | None = None) -> None:
    """Refuse to boot if the process looks like it wants a live broker."""
    env = dict(os.environ if environ is None else environ)

    mode = env.get("GROW_EXECUTION_MODE")
    if mode is not None and mode.strip() != "":
        normalize_execution_mode(mode)

    for key in _FORBIDDEN_ENV:
        raw = (env.get(key) or "").strip()
        if raw.lower() in _LIVE_TRUTH:
            raise GrowLiveTradingDisabled(
                f"Environment variable {key}={raw!r} requests live trading. Refusing to start."
            )
        # Presence of a broker token is itself a live-trading smell in m1.
        if key.endswith("_TOKEN") or key.endswith("_API_KEY"):
            if raw:
                raise GrowLiveTradingDisabled(
                    f"Environment variable {key} is set. Broker credentials are not allowed in milestone 1."
                )


def assert_paper_runtime(mode: str, live_trading_enabled: bool, venue: str | None = None) -> None:
    assert_paper_compiled()
    normalize_execution_mode(mode)
    if live_trading_enabled:
        raise GrowLiveTradingDisabled("live_trading_enabled=true is forbidden.")
    if venue is not None and venue.strip().upper() not in {"PAPER", "GROW_PAPER"}:
        raise GrowLiveTradingDisabled(
            f"Venue {venue!r} is not the paper ledger. Live venues are not compiled."
        )
