"""Deterministic research confidence. No LLM. No future bars.

Formula (v1), identical for BULLISH and BEARISH:

    confidence = clamp(
        (trend_alignment
         + indicator_strength
         + structure_confirmation
         + regime_alignment) / 4,
        0, 1,
    )

Each term is in [0, 1]. Magnitudes use abs(), so a 2% move up and a 2%
move down score the same. Missing inputs score 0.

Component meaning
-----------------
trend_alignment
    How cleanly price/EMAs agree with the research direction.
indicator_strength
    How far the trigger indicator is from a neutral level (EMA gap,
    |RSI-50|, |ROC|, extension beyond a breakout).
structure_confirmation
    Candle/structure quality (distance from EMA, body vs ATR, RSI extreme).
regime_alignment
    Does the classified regime support this style of signal?

Regime table (trend/momentum/breakout style)
    aligned trend     1.00
    LOW_VOLATILITY    0.55
    HIGH_VOLATILITY   0.40
    UNKNOWN           0.35
    RANGE             0.25
    opposite trend    0.00

Regime table (mean-reversion style)
    RANGE             1.00
    LOW_VOLATILITY    0.80
    UNKNOWN           0.45
    HIGH_VOLATILITY   0.25
    BULL/BEAR trend   0.00
"""

from __future__ import annotations

from typing import Mapping

from grow.strategies.models import Direction, MarketRegime

_TREND_REGIME = {
    Direction.BULLISH: {
        MarketRegime.BULL_TREND: 1.00,
        MarketRegime.LOW_VOLATILITY: 0.55,
        MarketRegime.HIGH_VOLATILITY: 0.40,
        MarketRegime.UNKNOWN: 0.35,
        MarketRegime.RANGE: 0.25,
        MarketRegime.BEAR_TREND: 0.00,
    },
    Direction.BEARISH: {
        MarketRegime.BEAR_TREND: 1.00,
        MarketRegime.LOW_VOLATILITY: 0.55,
        MarketRegime.HIGH_VOLATILITY: 0.40,
        MarketRegime.UNKNOWN: 0.35,
        MarketRegime.RANGE: 0.25,
        MarketRegime.BULL_TREND: 0.00,
    },
}

_REVERSION_REGIME = {
    MarketRegime.RANGE: 1.00,
    MarketRegime.LOW_VOLATILITY: 0.80,
    MarketRegime.UNKNOWN: 0.45,
    MarketRegime.HIGH_VOLATILITY: 0.25,
    MarketRegime.BULL_TREND: 0.00,
    MarketRegime.BEAR_TREND: 0.00,
}


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if value < lo else hi if value > hi else value


def unit(value: float, full: float) -> float:
    """Map |value| so `full` → 1.0. Symmetric. `full` must be > 0."""
    if full <= 0:
        return 0.0
    return clamp(abs(value) / full)


def regime_alignment(direction: Direction, regime: MarketRegime, *, style: str) -> float:
    if style == "reversion":
        return _REVERSION_REGIME[regime]
    return _TREND_REGIME[direction][regime]


def score_confidence(parts: Mapping[str, float]) -> float:
    if not parts:
        return 0.0
    mean = sum(clamp(float(v)) for v in parts.values()) / len(parts)
    return round(clamp(mean), 4)


def format_parts(parts: Mapping[str, float], confidence: float) -> str:
    body = " ".join(f"{k}={clamp(float(v)):.2f}" for k, v in parts.items())
    return f"confidence={confidence:.2f} [{body}]"
