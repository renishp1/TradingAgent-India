"""Phase 8 — campaign paper config helpers.

Applies the campaign paper profile without mutating global YAML defaults.
Default YAML now ships with INDIA_INDEX_OPTIONS_PAPER_10K values; this helper
still re-applies the named profile + conservative fills for explicit campaign
runners. LivePaperLoop remains available; this path targets PaperExecutionEngine only.
"""

from __future__ import annotations

from dataclasses import replace

from grow.config import (
    GrowConfig,
    apply_paper_capital_profile,
    load_config,
)

CAMPAIGN_CAPITAL_PROFILE = "INDIA_INDEX_OPTIONS_PAPER_10K"
CAMPAIGN_PRICE_MODE = "conservative"
# 0 = disabled: campaign lifetime is square-off / session-close, not LivePaperLoop's 30s.
CAMPAIGN_SESSION_TIMEOUT_SECONDS = 0


def campaign_paper_config(
    config: GrowConfig | None = None,
    *,
    profile: str = CAMPAIGN_CAPITAL_PROFILE,
    price_mode: str = CAMPAIGN_PRICE_MODE,
) -> GrowConfig:
    """Return a paper campaign config: named capital profile + conservative fills.

    Also clears the LivePaperLoop ``session_timeout_seconds`` inheritance so a
    reused ``PaperExecutionEngine`` is not halted after 30s. Intraday end is
    governed by market square-off (15:15) / session close (15:30).
    """
    base = config if config is not None else load_config()
    if profile:
        base = apply_paper_capital_profile(base, profile)
    mode = str(price_mode).strip().lower()
    return replace(
        base,
        paper=replace(base.paper, price_mode=mode),
        live_data=replace(
            base.live_data,
            session_timeout_seconds=CAMPAIGN_SESSION_TIMEOUT_SECONDS,
        ),
    )
