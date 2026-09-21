"""Deterministic market-regime classification. No LLM."""

from __future__ import annotations

from grow.strategies.indicators import closes, ema
from grow.strategies.models import IndicatorSnapshot, MarketRegime, RegimeSnapshot
from grow.data.schema import Bar


def _trend_label(price: float, fast: float | None, slow: float | None) -> str:
    if fast is None or slow is None:
        return "unknown"
    if fast > slow and price > fast:
        return "up"
    if fast < slow and price < fast:
        return "down"
    return "mixed"


def classify(
    *,
    m15: IndicatorSnapshot,
    d1_bars: tuple[Bar, ...],
    fast_period: int,
    slow_period: int,
    atr_period: int,
) -> RegimeSnapshot:
    d1_closes = closes(d1_bars)
    d1_fast = ema(d1_closes, min(fast_period, max(1, len(d1_closes)))) if len(d1_closes) >= fast_period else None
    d1_slow = ema(d1_closes, slow_period)
    d1_price = d1_closes[-1] if d1_closes else m15.close
    d1_trend = _trend_label(d1_price, d1_fast, d1_slow)
    m15_trend = _trend_label(m15.close, m15.ema_fast, m15.ema_slow)

    vol = "unknown"
    if m15.atr is not None and m15.close > 0:
        atr_pct = m15.atr / m15.close
        if atr_pct >= 0.012:
            vol = "high"
        elif atr_pct <= 0.004:
            vol = "low"
        else:
            vol = "mid"

    if vol == "high" and m15_trend == "mixed":
        label = MarketRegime.HIGH_VOLATILITY
    elif vol == "low" and m15_trend == "mixed":
        label = MarketRegime.LOW_VOLATILITY
    elif d1_trend == "up" and m15_trend == "up":
        label = MarketRegime.BULL_TREND
    elif d1_trend == "down" and m15_trend == "down":
        label = MarketRegime.BEAR_TREND
    elif m15_trend == "mixed" or d1_trend == "mixed":
        label = MarketRegime.RANGE
    else:
        label = MarketRegime.UNKNOWN

    reason = f"d1={d1_trend} m15={m15_trend} vol={vol}"
    if d1_fast is None or d1_slow is None:
        if m15.ema_fast is None or m15.ema_slow is None:
            label = MarketRegime.UNKNOWN
            reason = "INSUFFICIENT_HISTORY for regime EMAs"
        elif m15_trend == "up":
            label = MarketRegime.BULL_TREND
        elif m15_trend == "down":
            label = MarketRegime.BEAR_TREND
        else:
            label = MarketRegime.RANGE
    return RegimeSnapshot(
        label=label,
        d1_trend=d1_trend,
        m15_trend=m15_trend,
        volatility=vol,
        reason=reason,
    )
