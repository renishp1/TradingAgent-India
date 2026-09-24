#!/usr/bin/env python3
"""Phase 5 LIVE proof: NIFTY + SENSEX multi-index CampaignRunner (paper only).

Requires Kite credentials. Never places broker orders. Does not force a trade.

    set KITE_API_KEY=...
    set KITE_ACCESS_TOKEN=...
    set GROW_RISK_SECRET=...
    python scripts/run_sensex_live_proof.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grow.campaign import CampaignRunner, campaign_paper_config  # noqa: E402
from grow.clock import IST, SystemClock  # noqa: E402
from grow.config import apply_dotenv, load_config  # noqa: E402
from grow.errors import GrowConfigError  # noqa: E402
from grow.execution.lock import LIVE_TRADING_COMPILED, assert_paper_compiled  # noqa: E402
from grow.live_data.kite_campaign_snapshot import (  # noqa: E402
    CAMPAIGN_UNDERLYINGS,
    build_campaign_live_snapshot,
)
from grow.live_data.kite_market import RealKiteTransport, load_kite_market_secrets  # noqa: E402
from grow.risk.secret import resolve_risk_secret  # noqa: E402


def _market_open(now: datetime) -> bool:
    local = now.astimezone(IST)
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return (9 * 60 + 15) <= minutes <= (15 * 60 + 30)


def main() -> int:
    assert_paper_compiled()
    apply_dotenv()
    if LIVE_TRADING_COMPILED:
        print("LIVE_TRADING_COMPILED must be false", file=sys.stderr)
        return 2

    try:
        api_key, access_token = load_kite_market_secrets(os.environ)
        risk_secret = resolve_risk_secret()
    except GrowConfigError as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}))
        return 1

    clock = SystemClock()
    now = clock.now()
    if not _market_open(now):
        print(
            json.dumps(
                {
                    "status": "SKIPPED_MARKET_CLOSED",
                    "as_of": now.astimezone(IST).isoformat(),
                    "note": "Unit tests still validate SENSEX; LIVE proof requires session open.",
                    "LIVE_TRADING_COMPILED": False,
                },
                indent=2,
            )
        )
        return 0

    transport = RealKiteTransport(api_key=api_key, access_token=access_token)
    try:
        snapshot, summary = build_campaign_live_snapshot(
            transport,
            clock,
            underlyings=CAMPAIGN_UNDERLYINGS,
        )
    except GrowConfigError as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc), "summary": {}}, indent=2))
        return 3

    cfg = campaign_paper_config(load_config(environ={"GROW_EXECUTION_MODE": "paper"}))
    runner = CampaignRunner(
        cfg,
        clock=clock,
        risk_secret=risk_secret,
        apply_campaign_defaults=False,
    )
    result = runner.run_cycle(snapshot, cycle_id=f"sensex-live-{now.strftime('%H%M%S')}")

    camp = next(
        (o for o in result.package.agent_outputs if o.agent_name == "campaign_options"),
        None,
    )
    camp_metrics = dict(camp.calculated_metrics) if camp is not None else {}

    proof: dict[str, Any] = {
        "status": "OK",
        "as_of": now.astimezone(IST).isoformat(),
        "LIVE_TRADING_COMPILED": False,
        "broker_order_calls": result.execution.broker_order_calls,
        "broker_order_path": result.to_dict().get("broker_order_path"),
        "capital": {
            "starting_cash": cfg.paper.starting_cash,
            "max_per_trade_risk": cfg.risk.max_per_trade_risk,
            "max_daily_loss": cfg.risk.max_daily_loss,
        },
        "snapshot": {
            "snapshot_id": snapshot.snapshot_id,
            "underlyings": list(snapshot.underlyings.keys()),
            "option_count": len(snapshot.option_contracts),
            "provenance": snapshot.market_data_source,
            "paper_mode": snapshot.paper_mode,
            "live_trading": snapshot.live_trading,
        },
        "per_index_live": summary.get("per_index"),
        "index_errors": summary.get("index_errors"),
        "nifty_proof": _index_proof(snapshot, summary, "NIFTY"),
        "sensex_proof": _index_proof(snapshot, summary, "SENSEX"),
        "multi_index": {
            "both_present": set(snapshot.underlyings.keys()) >= {"NIFTY", "SENSEX"},
            "campaign_evaluated": camp_metrics.get("underlyings_evaluated"),
            "per_index_status": {
                k: (v or {}).get("status") for k, v in (camp_metrics.get("per_index") or {}).items()
            },
        },
        "candidate": {
            "action": str(camp.candidate_action) if camp else None,
            "instrument": camp.candidate_instrument if camp else None,
            "underlying": camp_metrics.get("underlying"),
            "required_cash": camp_metrics.get("required_cash"),
            "planned_risk": camp_metrics.get("planned_risk"),
            "feasibility_reason": camp_metrics.get("feasibility_reason"),
        },
        "decision": {
            "status": result.decision.status.value,
            "action": result.decision.action.value,
            "risk_guard_result": result.decision.risk_guard_result,
            "reason_codes": list(result.decision.reason_codes),
        },
        "execution": {
            "accepted": result.execution.accepted,
            "reason": result.execution.reason,
            "fills": len(result.execution.fills) if getattr(result.execution, "fills", None) else 0,
            "broker_order_calls": result.execution.broker_order_calls,
        },
    }
    out = ROOT / "results" / f"sensex-live-proof-{now.strftime('%Y%m%dT%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(proof, indent=2, default=str), encoding="utf-8")
    print(json.dumps(proof, indent=2, default=str))
    print(f"wrote={out}", file=sys.stderr)

    if not proof["multi_index"]["both_present"]:
        return 4
    if proof["broker_order_calls"] != 0:
        return 5
    return 0


def _index_proof(snapshot, summary, name: str) -> dict[str, Any]:
    per = (summary.get("per_index") or {}).get(name) or {}
    quote = snapshot.underlyings.get(name)
    opts = [o for o in snapshot.option_contracts if o.underlying == name]
    hist = (snapshot.diagnostics or {}).get("history_closes_by_underlying") or {}
    closes = hist.get(name) or []
    return {
        "present": name in snapshot.underlyings,
        "spot": None if quote is None else quote.spot or quote.ltp,
        "exchange": None if quote is None else quote.exchange,
        "m15_bars": len(closes),
        "m15_provenance": per.get("history_provenance") or snapshot.diagnostics.get("history_provenance"),
        "option_count": len(opts),
        "lot_sizes": sorted({int(o.lot_size) for o in opts if o.lot_size}),
        "expiry": per.get("expiry"),
        "live_meta": per,
    }


if __name__ == "__main__":
    raise SystemExit(main())
