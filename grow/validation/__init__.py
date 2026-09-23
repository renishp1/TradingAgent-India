"""Walk-forward validation — historical and out-of-sample evaluation.

Paper-only. No live broker execution. No tuning on the final evaluation window.
Replays snapshot → agent/decision → paper-execution contracts against
point-in-time historical data with rolling train/validation/test windows.
"""

from grow.validation.historical import (
    HISTORICAL_VALIDATION_VERSION,
    HistoricalValidationCampaign,
    HistoricalValidationResult,
    assert_historical_evaluation_provenance,
    build_historical_agent_snapshot,
    build_historical_cycles,
    listed_contracts_from_store,
    snapshot_option_provenance,
)
from grow.validation.labels import (
    EvaluationLabel,
    assert_no_fixture_profitability_claim,
    assert_single_evaluation_label,
    classify_store_evaluation_label,
    require_historical_evaluation_dataset,
)
from grow.validation.models import EvaluationRun, WalkForwardPlan, WalkWindow
from grow.validation.runner import WalkForwardValidationRunner, WalkForwardValidationResult
from grow.validation.windows import split_walk_windows

__all__ = [
    "HISTORICAL_VALIDATION_VERSION",
    "EvaluationLabel",
    "EvaluationRun",
    "HistoricalValidationCampaign",
    "HistoricalValidationResult",
    "WalkForwardPlan",
    "WalkForwardValidationResult",
    "WalkForwardValidationRunner",
    "WalkWindow",
    "assert_historical_evaluation_provenance",
    "assert_no_fixture_profitability_claim",
    "assert_single_evaluation_label",
    "build_historical_agent_snapshot",
    "build_historical_cycles",
    "classify_store_evaluation_label",
    "listed_contracts_from_store",
    "require_historical_evaluation_dataset",
    "snapshot_option_provenance",
    "split_walk_windows",
]
