#!/usr/bin/env python3
"""Fixture campaign cycle: snapshot → 4B → 4C → Risk → PaperExecutionEngine.

Paper-only. No broker. Uses campaign conservative / ₹10K helpers without
mutating global YAML defaults. LivePaperLoop is not used on this path.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grow.campaign import CampaignRunner, campaign_paper_config  # noqa: E402
from grow.clock import IST, FrozenClock  # noqa: E402
from grow.config import load_config  # noqa: E402
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction  # noqa: E402
from grow.errors import GrowConfigError  # noqa: E402
from grow.execution.lock import assert_paper_compiled, inspect_environment  # noqa: E402
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView  # noqa: E402
from grow.market_data.snapshots.builder import build_fixture_snapshot  # noqa: E402
from grow.risk.secret import resolve_risk_secret  # noqa: E402


AS_OF = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
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
            entry_reason="fixture",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
            confidence=0.5,
            cycle_id=cycle_id,
        )

    def analyze_input(self, agent_input):
        return self.analyze(agent_input.snapshot, cycle_id=agent_input.cycle_id)


def main(argv: list[str] | None = None) -> int:
    del argv
    assert_paper_compiled()
    inspect_environment()
    try:
        risk_secret = resolve_risk_secret()
    except GrowConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    config = campaign_paper_config(load_config())
    clock = FrozenClock(AS_OF)
    option = OptionQuoteView(
        underlying="RELIANCE",
        expiry=EXPIRY,
        strike=2500.0,
        option_type="CE",
        ltp=100.0,
        bid=99.0,
        ask=101.0,
        open_interest=10,
        volume=10,
        quote_timestamp=AS_OF,
        quote_age_seconds=0.0,
        provider_contract_id="RELIANCE-2500-CE",
        quality=DataQualityStatus.OK,
        lot_size=1,
        expiry_class="WEEKLY",
    )
    snapshot = build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=AS_OF,
        spot=2500.0,
        option_contracts=(option,),
        notes=("fixture campaign cycle",),
    )
    runner = CampaignRunner(
        config,
        clock=clock,
        specialists=(_DemoSpecialist(),),
        risk_secret=risk_secret,
        apply_campaign_defaults=False,
    )
    result = runner.run_cycle(snapshot, cycle_id="cycle-campaign-fixture")
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    return 0 if result.execution.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
