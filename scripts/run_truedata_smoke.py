#!/usr/bin/env python3
"""Credential-gated TrueData smoke test. Paper only. Disabled in CI by default.

Usage (never commit secrets):

    export LIVE_DATA_ENABLED=true
    export LIVE_DATA_PROVIDER=truedata
    export GROW_LIVE_DATA_MODE=real
    export TRUEDATA_USERNAME=...
    export TRUEDATA_PASSWORD=...
    export TRUEDATA_SMOKE=1
    python scripts/run_truedata_smoke.py

Flow:
    credentials → WebSocket auth → vendor catalog discovery (2I overlay)
    → ATM subscription → first live snapshot → 3A/3B paper loop diagnostics

The catalog is fetched from TrueData symbol lists. Do not inject a fixture
catalog. Do not set any broker token or live_trading flag.
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
        provider = loop.provider
        catalog = list(getattr(provider, "instrument_catalog", lambda: ())())
        discovered = list(getattr(provider, "discover_underlyings", lambda: ())())
        subscribed = list(provider.health().subscribed)
        reports = []
        for _ in range(64):
            batch = loop.run_once()
            reports.extend(batch)
            if loop.last_snapshot is not None:
                break
            reason = batch[-1].reason if batch else ""
            if reason.startswith("FEED_"):
                continue
            break
        snapshot = None if loop.last_snapshot is None else loop.last_snapshot.to_dict()
        print(
            json.dumps(
                {
                    "health": loop.health.to_dict(),
                    "catalog_size": len(catalog),
                    "discovered_underlyings": discovered,
                    "subscribed": subscribed or list(provider.health().subscribed),
                    "snapshot": snapshot,
                    "reports": [row.to_dict() for row in reports],
                    "paper_only": True,
                    "live_trading": False,
                    "broker": False,
                    "catalog_injected": False,
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
