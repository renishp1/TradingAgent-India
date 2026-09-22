"""Engineering-supplied 2A–2E capabilities. CEO cannot invent parameters."""

from __future__ import annotations

from grow.director.models import CONFIG_CANDIDATE, ConfigCandidate

HARD_LOCKS = (
    "same_day_expiry_forbidden",
    "buy_ce_pe_only",
    "bullish_ce",
    "bearish_pe",
    "no_option_selling",
    "nifty_banknifty_only",
    "paper_only",
    "no_broker",
    "calibration_mode_none",
)

DEFAULT_CANDIDATE = ConfigCandidate(
    config_id=CONFIG_CANDIDATE,
    version="v1",
    description="Frozen default 2B/2C/2D grow.default.yaml",
)

DEFAULT_STRESS = (
    "base_cost_slippage",
    "cost_x2",
    "slippage_x2",
    "ce_vs_pe",
    "nifty_vs_banknifty",
)

DEFAULT_METRICS = (
    "net_pnl",
    "gross_pnl",
    "expectancy",
    "max_drawdown",
    "trade_count",
    "no_trade_rate",
)

DEFAULT_ACCEPTANCE = (
    "min_test_trades",
    "positive_net_expectancy",
    "max_drawdown_ceiling",
    "bounded_cost_sensitivity",
    "no_window_dominance",
    "no_major_data_quality_gap",
    "no_leakage",
    "stable_across_windows",
    "fixture_data_not_accept_for_paper",
)

DEFAULT_EXCLUSIONS = (
    "incomplete_coverage",
    "leakage",
    "missing_manifest",
    "unapproved_dataset",
)


def candidate_by_id(config_id: str) -> ConfigCandidate | None:
    if config_id == CONFIG_CANDIDATE:
        return DEFAULT_CANDIDATE
    return None
