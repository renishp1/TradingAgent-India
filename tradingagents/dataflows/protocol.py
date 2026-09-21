"""Dataflow protocol. No vendor clients in milestone 1."""

from __future__ import annotations

from typing import Any, Protocol


class MarketDataPort(Protocol):
    def quote(self, ticker: str, as_of: str) -> dict[str, Any]: ...


class StubMarketData:
    def quote(self, ticker: str, as_of: str) -> dict[str, Any]:
        return {"ticker": ticker, "as_of": as_of, "source": "stub", "available": False}
