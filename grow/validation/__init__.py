"""Walk-forward validation — historical and out-of-sample evaluation.

Paper-only. No live broker execution. No tuning on the final evaluation window.
Replays snapshot → agent/decision → paper-execution contracts against
point-in-time historical data with rolling train/validation/test windows.
"""

from grow.validation.models import EvaluationRun, WalkForwardPlan, WalkWindow
from grow.validation.runner import WalkForwardValidationRunner, WalkForwardValidationResult
from grow.validation.windows import split_walk_windows

__all__ = [
    "EvaluationRun",
    "WalkForwardPlan",
    "WalkForwardValidationResult",
    "WalkForwardValidationRunner",
    "WalkWindow",
    "split_walk_windows",
]
