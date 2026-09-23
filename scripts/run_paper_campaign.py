#!/usr/bin/env python3
"""Multi-session fixture paper campaign with per-session EOD reports.

Paper-only. No broker. Uses Phase 13 ``PaperCampaign`` over existing session /
campaign / paper engines. For LIVE-PAPER, supply real Zerodha snapshots externally
— this demo uses FIXTURE evaluation labeling.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grow.campaign import PaperCampaign, SessionFeed, campaign_paper_config  # noqa: E402
from grow.clock import IST  # noqa: E402
from grow.config import load_config  # noqa: E402
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction  # noqa: E402
from grow.execution.lock import assert_paper_compiled, inspect_environment  # noqa: E402
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView  # noqa: E402
from grow.market_data.snapshots.builder import build_fixture_snapshot  # noqa: E402
from grow.risk.secret import resolve_risk_secret  # noqa: E402


EXPIRY = date(2026, 9, 24)


class _DemoSpecialist:
    agent_name = "strategy_research"
    agent_version = "strategy_research.v2"

    def analyze(self, snapshot, *, cycle_id: str = ""):
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=("fixture campaign",),
            calculated_metrics={
                "strategy": "trend",
                "direction": "BULLISH",
                "underlying": "RELIANCE",
                "limit_price": 100.0,
                "stop_loss": 80.0,
                "target": 140.0,
                "lots": 1,
                "quantity": 1,
                "option_type": "CE",
                "strike": 2500.0,
                "expiry": EXPIRY.isoformat(),
                "lot_size": 1,
            },
            interpretation=(),
            findings=("UNANIMOUS_OPEN",),
            data_quality_concerns=(),
            assumptions=("buyer-only",),
            evidence=(f"snapshot_id={snapshot.snapshot_id}",),
            metrics_used=("limit_price", "stop_loss"),
            candidate_action=CandidateAction.PAPER_OPEN,
            candidate_instrument="RELIANCE-2500-CE",
            entry_reason="fixture-campaign",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
            confidence=0.5,
            cycle_id=cycle_id,
        )

    def analyze_input(self, agent_input):
        return self.analyze(agent_input.snapshot, cycle_id=agent_input.cycle_id)


def _quote(as_of, **overrides):
    payload = dict(
        underlying="RELIANCE",
        expiry=EXPIRY,
        strike=2500.0,
        option_type="CE",
        ltp=100.0,
        bid=99.0,
        ask=101.0,
        open_interest=10,
        volume=10,
        quote_timestamp=as_of,
        quote_age_seconds=0.0,
        provider_contract_id="RELIANCE-2500-CE",
        quality=DataQualityStatus.OK,
        lot_size=1,
        expiry_class="WEEKLY",
    )
    payload.update(overrides)
    return OptionQuoteView(**payload)


def _session_feed(day: date) -> SessionFeed:
    open_at = datetime(day.year, day.month, day.day, 11, 0, tzinfo=IST)
    later = open_at + timedelta(minutes=5)
    return SessionFeed(
        session_date=day,
        snapshots=(
            build_fixture_snapshot(
                underlying="RELIANCE",
                as_of=open_at,
                spot=2500.0,
                option_contracts=(_quote(open_at),),
            ),
            build_fixture_snapshot(
                underlying="RELIANCE",
                as_of=later,
                spot=2500.0,
                option_contracts=(_quote(later, ltp=110.0, bid=109.0, ask=111.0),),
            ),
        ),
    )


def main() -> int:
    assert_paper_compiled()
    inspect_environment()
    try:
        secret = resolve_risk_secret()
    except Exception as exc:  # noqa: BLE001 — demo fail-closed
        print(json.dumps({"error": str(exc), "hint": "set GROW_RISK_SECRET"}, indent=2))
        return 2

    config = campaign_paper_config(load_config())
    with tempfile.TemporaryDirectory(prefix="grow-paper-campaign-") as tmp:
        campaign = PaperCampaign(
            config,
            risk_secret=secret,
            evaluation_label="FIXTURE",
            artifact_dir=tmp,
            specialists=(_DemoSpecialist(),),
            apply_campaign_defaults=False,
        )
        feeds = (
            _session_feed(date(2026, 9, 21)),
            _session_feed(date(2026, 9, 22)),
            _session_feed(date(2026, 9, 23)),
        )
        report = campaign.run(feeds)
        out = campaign.write_report(Path(tmp) / "campaign-report.json")
        print(
            json.dumps(
                {
                    "campaign_id": report.campaign_id,
                    "evaluation_label": report.evaluation_label,
                    "session_count": report.session_count,
                    "eod_paths": list(report.eod_paths),
                    "report_path": str(out),
                    "broker_order_calls": 0,
                    "profitability_claim": False,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
