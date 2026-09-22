"""Fixture-driven multi-agent paper analysis cycle (Requirement 4B).

No broker. No live trading. Emits an auditable AggregateAnalysisPackage.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grow.agents.orchestrator import AgentCycleOrchestrator  # noqa: E402
from grow.clock import IST  # noqa: E402
from grow.execution.lock import assert_paper_compiled, inspect_environment  # noqa: E402
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView  # noqa: E402
from grow.market_data.snapshots.builder import build_fixture_snapshot  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    assert_paper_compiled()
    inspect_environment()
    as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
    option = OptionQuoteView(
        underlying="NIFTY",
        expiry=as_of.date(),
        strike=25000.0,
        option_type="CE",
        ltp=120.5,
        bid=119.0,
        ask=122.0,
        open_interest=1500,
        volume=200,
        quote_timestamp=as_of,
        quote_age_seconds=0.0,
        provider_contract_id="NIFTY-fixture-CE",
        quality=DataQualityStatus.OK,
    )
    snapshot = build_fixture_snapshot(
        underlying="NIFTY",
        as_of=as_of,
        spot=25000.0,
        option_contracts=(option,),
        notes=("fixture agent cycle",),
        diagnostics={"history_closes": [24900.0 + i * 5 for i in range(20)]},
    )
    decision = AgentCycleOrchestrator(configured_strategies=("trend", "momentum")).run(snapshot)
    print(json.dumps(decision.to_dict(), indent=2, sort_keys=True))
    return 0 if decision.decision in {"NO_TRADE", "REJECTED_BY_RISK", "ANALYSIS_COMPLETE"} else 1


if __name__ == "__main__":
    sys.exit(main())
