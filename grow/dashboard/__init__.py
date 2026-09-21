"""Dashboard snapshot schema.

Milestone 1 does not ship a web server. This module is the contract a later
console can consume. The Grok architecture console mirrors the same fields.
"""

from __future__ import annotations

from typing import Any

from grow.config import GrowConfig
from grow.cycle import CycleReport
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.paper.ledger import PaperBook


def lock_status() -> dict[str, Any]:
    return {
        "live_trading_compiled": LIVE_TRADING_COMPILED,
        "execution_mode": "paper",
        "live_venues": [],
        "message": "Paper-trading only. Live brokerage is not compiled.",
    }


def snapshot(config: GrowConfig, book: PaperBook, last_cycle: CycleReport | None = None) -> dict[str, Any]:
    return {
        "lock": lock_status(),
        "config": {
            "version": config.version,
            "timezone": config.timezone,
            "exchange": config.market.exchange,
            "square_off": config.market.square_off,
            "universe": list(config.market.universe),
            "model_provider": config.model.provider,
        },
        "book": book.snapshot(),
        "last_cycle": None if last_cycle is None else last_cycle.to_dict(),
    }
