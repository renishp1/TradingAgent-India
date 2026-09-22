#!/usr/bin/env python3
"""Credential-gated TrueData 3C.2 smoke. Paper only. Disabled unless TRUEDATA_SMOKE=1.

Usage (never commit secrets):

    export TRUEDATA_SMOKE=1
    export TRUEDATA_USERNAME=...
    export TRUEDATA_PASSWORD=...
    export GROW_RISK_SECRET=...
    python scripts/run_truedata_smoke.py

Flow:
    credentials → WebSocket auth → vendor catalog discovery (2I overlay)
    → 3C.1 expiry classification → ATM subscription → mapping_ready
    → first live option quote → 3A/3B paper loop diagnostics → smoke report

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
from grow.errors import GrowConfigError, GrowLiveTradingDisabled  # noqa: E402
from grow.execution.lock import LIVE_TRADING_COMPILED  # noqa: E402
from grow.live_data.loop import open_loop  # noqa: E402
from grow.live_data.smoke import (  # noqa: E402
    HARD_FAIL,
    PASS,
    PASS_WITH_NO_TRADE,
    build_smoke_report,
    drain_smoke_loop,
    load_smoke_secrets,
    smoke_config,
    smoke_risk_secret,
    write_smoke_report,
)


def _report_path(session_id: str) -> Path:
    return ROOT / "results" / f"truedata-smoke-{session_id}.json"


def _print_report(report: dict, path: Path) -> int:
    print(json.dumps(report, indent=2, default=str))
    print(f"smoke_report={path}", file=sys.stderr)
    if report["result"] in {PASS, PASS_WITH_NO_TRADE}:
        return 0
    if report["result"] == HARD_FAIL:
        return 1
    return 2


def main(environ: dict[str, str] | None = None) -> int:
    env = dict(os.environ if environ is None else environ)
    if LIVE_TRADING_COMPILED:
        print("LIVE_TRADING_COMPILED forbids this binary", file=sys.stderr)
        return 1
    secrets: tuple[str, ...] = ()
    loop = None
    reports: list = []
    error = None
    try:
        user, password = load_smoke_secrets(env)
        secrets = tuple(item for item in (user, password, env.get("GROW_RISK_SECRET", "")) if item)
        config = smoke_config(env)
        secret = smoke_risk_secret(env)
        secrets = tuple(item for item in (*secrets, secret) if item)
        loop = open_loop(config, clock=SystemClock(), risk_secret=secret)
        try:
            loop.start()
            reports = drain_smoke_loop(loop)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            error = str(exc)
            reports = list(getattr(loop, "cycles", ()) or reports)
        try:
            loop.stop()
        except Exception:
            pass
        report = build_smoke_report(loop, reports, secrets=secrets, root=ROOT, error=error)
        path = write_smoke_report(report, _report_path(loop.session.session_id))
        return _print_report(report, path)
    except (GrowConfigError, GrowLiveTradingDisabled) as exc:
        message = str(exc)
        for token in secrets:
            if token and token in message:
                message = message.replace(token, "[REDACTED]")
        print(message, file=sys.stderr)
        if loop is not None:
            report = build_smoke_report(loop, reports, secrets=secrets, root=ROOT, error=message)
            path = write_smoke_report(report, _report_path(loop.session.session_id))
            return _print_report(report, path)
        if "LIVE_" in message or "BROKER" in message:
            return 1
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
