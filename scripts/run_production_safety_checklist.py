#!/usr/bin/env python3
"""Run the post-Phase-13 production safety checklist and print JSON.

Paper-only audit. Does not place broker orders. Does not enable live trading.
Always reports live_trading_qualification_ready=false while live Zerodha
proofs remain BLOCKED (AUTH_MISSING in this environment).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grow.execution.lock import (  # noqa: E402
    LIVE_TRADING_COMPILED,
    assert_paper_compiled,
    inspect_environment,
)
from grow.safety import run_production_safety_checklist  # noqa: E402


def main() -> int:
    assert_paper_compiled()
    inspect_environment()
    report = run_production_safety_checklist()
    payload = report.to_dict()
    payload["environment"] = {
        "live_trading_compiled": LIVE_TRADING_COMPILED,
        "paper_mode": True,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    # Exit non-zero only on FAIL; BLOCKED live proofs are expected.
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
