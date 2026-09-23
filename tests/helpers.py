"""Shared test fixtures. Not collected by unittest discover."""

from __future__ import annotations

from dataclasses import replace

from grow.config import GrowConfig, load_config
from grow.cycle import GrowRuntime
from grow.risk.guard import RiskGuard

TEST_RISK_SECRET = "grow-test-hmac-v1"


def make_guard(config, clock=None) -> RiskGuard:
    return RiskGuard(config, clock=clock, secret=TEST_RISK_SECRET)


def make_runtime(config, clock=None) -> GrowRuntime:
    return GrowRuntime(config, clock=clock, risk_secret=TEST_RISK_SECRET)


def research_fixture_config(config: GrowConfig | None = None) -> GrowConfig:
    """Capital sized for LivePaperLoop / M1 cash fixtures (not the ₹10K operator profile).

    Operator defaults in ``grow.default.yaml`` are INDIA_INDEX_OPTIONS_PAPER_10K.
    Legacy 3A / cash-probe tests need a larger book and no per-trade risk cap.
    """
    base = config if config is not None else load_config(environ={"GROW_EXECUTION_MODE": "paper"})
    return replace(
        base,
        paper=replace(
            base.paper,
            starting_cash=1_000_000,
            capital_profile=None,
            price_mode=None,
        ),
        risk=replace(
            base.risk,
            max_daily_loss=15_000,
            max_per_trade_risk=None,
            max_open_positions=None,
            max_symbol_concentration=0.35,
        ),
    )
