#!/usr/bin/env python3
"""Credential-gated TrueData smoke test. Paper only. Disabled in CI by default.

Usage (never commit secrets):

    export LIVE_DATA_ENABLED=true
    export LIVE_DATA_PROVIDER=truedata
    export GROW_LIVE_DATA_MODE=real
    export TRUEDATA_USERNAME=...
    export TRUEDATA_PASSWORD=...
    python scripts/run_truedata_smoke.py

Do not set broker or live-trading flags. Expected result: feed connects,
paper diagnostics are printed, no real order exists.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grow.clock import SystemClock  # noqa: E402
from grow.config import load_config  # noqa: E402
from grow.errors import GrowConfigError  # noqa: E402
from grow.execution.lock import LIVE_TRADING_COMPILED  # noqa: E402
from grow.live_data.loop import open_loop  # noqa: E402
from grow.live_data.truedata import load_truedata_secrets  # noqa: E402


def main() -> int:
    if LIVE_TRADING_COMPILED:
        print("LIVE_TRADING_COMPILED forbids this binary", file=sys.stderr)
        return 1
    if not os.environ.get("TRUEDATA_SMOKE") and not os.environ.get("TRUEDATA_USERNAME"):
        print("AUTH_MISSING: set TRUEDATA_USERNAME / TRUEDATA_PASSWORD and TRUEDATA_SMOKE=1", file=sys.stderr)
        return 2
    try:
        load_truedata_secrets()
        config = load_config(
            environ={
                **os.environ,
                "GROW_LIVE_DATA_ENABLED": "true",
                "GROW_LIVE_DATA_PROVIDER": "truedata",
                "GROW_LIVE_DATA_MODE": "real",
                "LIVE_DATA_ENABLED": "true",
                "LIVE_DATA_PROVIDER": "truedata",
            }
        )
        if config.live_data.live_trading or config.execution.live_trading_enabled:
            print("LIVE_EXECUTION_FORBIDDEN", file=sys.stderr)
            return 1
        loop = open_loop(config, clock=SystemClock())
        loop.start()
        reports = loop.run_once()
        print(
            json.dumps(
                {
                    "health": loop.health.to_dict(),
                    "reports": [row.to_dict() for row in reports],
                    "paper_only": True,
                    "live_trading": False,
                    "broker": False,
                },
                indent=2,
                default=str,
            )
        )
        loop.stop()
    except GrowConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
