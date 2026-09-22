"""Phase 8 — campaign paper config helpers.

Applies the campaign paper profile without mutating global YAML defaults.
LivePaperLoop remains available; this path targets PaperExecutionEngine only.
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


def campaign_paper_config(
    config: GrowConfig | None = None,
    *,
    profile: str = CAMPAIGN_CAPITAL_PROFILE,
    price_mode: str = CAMPAIGN_PRICE_MODE,
) -> GrowConfig:
    """Return a paper campaign config: named capital profile + conservative fills."""
    base = config if config is not None else load_config()
    if profile:
        base = apply_paper_capital_profile(base, profile)
    mode = str(price_mode).strip().lower()
    return replace(base, paper=replace(base.paper, price_mode=mode))
