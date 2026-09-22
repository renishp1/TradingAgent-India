#!/usr/bin/env python3
"""Credential-gated Zerodha 3C.3 smoke. Paper only. Disabled unless ZERODHA_SMOKE=1.

Usage (never commit secrets):

    export ZERODHA_SMOKE=1
    export KITE_API_KEY=...
    export KITE_ACCESS_TOKEN=...
    export GROW_RISK_SECRET=...
    python scripts/run_zerodha_smoke.py

The access token is read here and passed only to the market-data adapter.
It is removed before the paper boot lock runs. This command does not place
orders, does not call an order route, and does not inject ticks.
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
from grow.live_data.kite_market import KiteMarketProvider, load_kite_market_secrets  # noqa: E402
from grow.live_data.loop import LivePaperLoop  # noqa: E402
from grow.live_data.smoke import (  # noqa: E402
    HARD_FAIL,
    PASS,
    PASS_WITH_NO_TRADE,
    build_smoke_report,
    drain_smoke_loop,
    format_option_tick_evidence,
    smoke_risk_secret,
    kite_market_smoke_config,
    write_smoke_report,
)


def _report_path(session_id: str) -> Path:
    return ROOT / "results" / f"zerodha-smoke-{session_id}.json"


def _print_report(report: dict, path: Path) -> int:
    print(json.dumps(report, indent=2, default=str))
    print(format_option_tick_evidence(report), file=sys.stderr)
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
        config = kite_market_smoke_config(env)
        api_key, access_token = load_kite_market_secrets(env)
        secrets = tuple(item for item in (api_key, access_token, env.get("GROW_RISK_SECRET", "")) if item)
        secret = smoke_risk_secret({key: value for key, value in env.items() if key not in {"KITE_API_KEY", "KITE_ACCESS_TOKEN"}})
        secrets = tuple(item for item in (*secrets, secret) if item)
        clock = SystemClock()
        provider = KiteMarketProvider(api_key=api_key, access_token=access_token, clock=clock)
        loop = LivePaperLoop(config, provider, clock=clock, risk_secret=secret)
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
