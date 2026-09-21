"""Execution package. Paper is the only venue. Live is a hard error."""

from grow.execution.live import LiveBroker, place_live_order
from grow.execution.lock import (
    ALLOWED_EXECUTION_MODES,
    LIVE_TRADING_COMPILED,
    assert_paper_compiled,
    assert_paper_runtime,
    inspect_environment,
    normalize_execution_mode,
)

__all__ = [
    "ALLOWED_EXECUTION_MODES",
    "LIVE_TRADING_COMPILED",
    "LiveBroker",
    "assert_paper_compiled",
    "assert_paper_runtime",
    "inspect_environment",
    "normalize_execution_mode",
    "place_live_order",
]
