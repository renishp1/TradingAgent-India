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
        "research_label": "HISTORICAL RESEARCH / NOT LIVE",
    }


def provider_evaluation_view(store=None, result=None) -> dict[str, Any]:
    from grow.history.candidates import PUBLIC_CANDIDATES
    from grow.history.eval import ProviderEvaluationRunner
    from grow.history.sample_2i import SAMPLE_2I_ID, build_2i_store
    from grow.history.scorecard import scorecard_from

    sample = store or build_2i_store()
    evaluation = result or ProviderEvaluationRunner().evaluate(sample)
    card = scorecard_from(sample, evaluation)
    return {
        "label": "HISTORICAL RESEARCH / NOT LIVE",
        "live": False,
        "dataset_id": sample.meta.dataset_id if store is not None else SAMPLE_2I_ID,
        "scorecard": card.to_dict(),
        "qualification_status": evaluation.qualification_status,
        "approved_for_2e": evaluation.approved_for_2e,
        "public_candidates": [c.to_dict() for c in PUBLIC_CANDIDATES],
        "checks": [c.to_dict() for c in evaluation.checks],
    }

