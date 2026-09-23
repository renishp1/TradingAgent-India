"""Serve the Phase 1 read-only paper dashboard."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Grow paper-trading dashboard (read-only, no broker orders)."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default 8765)")
    parser.add_argument(
        "--checkpoint",
        default=None,
        help="Optional paper checkpoint JSON to display (read-only).",
    )
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is required for the dashboard. Install with:\n"
            "  .venv\\Scripts\\python.exe -m pip install -e .\n",
            file=sys.stderr,
        )
        return 1

    from grow.dashboard.app import create_app
    from grow.dashboard.service import build_service_from_environ

    service = build_service_from_environ(checkpoint_path=args.checkpoint)
    app = create_app(service)

    print("TradingAgent-India dashboard (PAPER MODE, read-only)")
    print(f"Open http://{args.host}:{args.port}/")
    print("LIVE TRADING DISABLED · BROKER ORDERS DISABLED")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
