#!/usr/bin/env python3
"""Create a local .env for paper trading (secret + paper flags only)."""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"


def main() -> int:
    if ENV_PATH.exists():
        print(f"Already exists: {ENV_PATH}")
        print("Edit GROW_RISK_SECRET there if needed; refusing to overwrite.")
        return 0

    secret = secrets.token_hex(32)
    if EXAMPLE_PATH.is_file():
        text = EXAMPLE_PATH.read_text(encoding="utf-8")
        if "# GROW_RISK_SECRET=" in text:
            text = text.replace("# GROW_RISK_SECRET=", f"GROW_RISK_SECRET={secret}", 1)
        else:
            text = text.rstrip() + f"\n\nGROW_RISK_SECRET={secret}\n"
    else:
        text = (
            "GROW_EXECUTION_MODE=paper\n"
            "GROW_LIVE_TRADING=false\n"
            "LIVE_TRADING_ENABLED=false\n"
            f"GROW_RISK_SECRET={secret}\n"
            "GROW_STARTING_CASH=10000\n"
            "GROW_MODEL_PROVIDER=mock\n"
            "GROW_DATA_PROVIDER=fixture\n"
            "GROW_ALLOW_LIVE_FEED=false\n"
        )

    ENV_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {ENV_PATH}")
    print("Paper mode only. Live trading flags stay false.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
