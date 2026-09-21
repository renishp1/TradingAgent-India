"""Versioned India F&O research cost + ask-side slippage. Not a broker."""

from __future__ import annotations

from grow.backtest.models import COST_MODEL, SLIP_MODEL


class CostModel:
    version = COST_MODEL
    brokerage_per_order = 20.0
    stt_sell_pct = 0.001
    exchange_pct = 0.00053
    gst_pct = 0.18

    def __init__(self, *, stress: float = 1.0) -> None:
        self.stress = stress

    def round_trip(self, *, entry: float, exit: float, quantity: int) -> float:
        buy_notional = entry * quantity
        sell_notional = exit * quantity
        brokerage = (self.brokerage_per_order * 2) * self.stress
        stt = sell_notional * self.stt_sell_pct * self.stress
        exchange = (buy_notional + sell_notional) * self.exchange_pct * self.stress
        gst = (brokerage + exchange) * self.gst_pct
        return round(brokerage + stt + exchange + gst, 4)


class SlippageModel:
    version = SLIP_MODEL

    def __init__(self, bps: float, *, stress: float = 1.0) -> None:
        self.bps = bps * stress

    def buy(self, ask: float) -> tuple[float, float]:
        slip = round(ask * self.bps / 10_000.0, 4)
        return round(ask + slip, 4), slip

    def sell(self, bid: float) -> tuple[float, float]:
        slip = round(bid * self.bps / 10_000.0, 4)
        px = round(bid - slip, 4)
        return max(0.05, px), slip
