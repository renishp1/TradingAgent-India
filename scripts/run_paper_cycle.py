#!/usr/bin/env python3
"""Run a single paper cycle against the default universe name."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grow.config import load_config  # noqa: E402
from grow.cycle import GrowRuntime  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grow paper-trading cycle (no live orders).")
    parser.add_argument("symbol", nargs="?", default="RELIANCE")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    runtime = GrowRuntime(config)
    report = runtime.run(args.symbol)
    print(json.dumps(report.to_dict(), indent=2, default=str))
    if report.fill is None:
        print("\nNo fill. Risk Guard reason:", report.verdict.reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
