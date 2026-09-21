"""Shared test fixtures. Not collected by unittest discover."""

from __future__ import annotations

from grow.cycle import GrowRuntime
from grow.risk.guard import RiskGuard

TEST_RISK_SECRET = "grow-test-hmac-v1"


def make_guard(config, clock=None) -> RiskGuard:
    return RiskGuard(config, clock=clock, secret=TEST_RISK_SECRET)


def make_runtime(config, clock=None) -> GrowRuntime:
    return GrowRuntime(config, clock=clock, risk_secret=TEST_RISK_SECRET)
