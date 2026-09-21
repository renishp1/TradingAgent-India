"""Deterministic indicators. Operate only on bars already in the snapshot."""

from __future__ import annotations

from grow.data.schema import Bar


def closes(bars: tuple[Bar, ...]) -> tuple[float, ...]:
    return tuple(bar.close for bar in bars)


def sma(values: tuple[float, ...] | list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return round(sum(window) / period, 6)


def ema(values: tuple[float, ...] | list[float], period: int) -> float | None:
    series = ema_series(values, period)
    if not series:
        return None
    return series[-1]


def ema_series(values: tuple[float, ...] | list[float], period: int) -> tuple[float, ...]:
    if period <= 0 or len(values) < period:
        return ()
    seed = sum(values[:period]) / period
    k = 2.0 / (period + 1)
    out = [seed]
    for value in values[period:]:
        seed = value * k + seed * (1.0 - k)
        out.append(seed)
    return tuple(round(v, 6) for v in out)


def ema_slope(values: tuple[float, ...] | list[float], period: int) -> float | None:
    series = ema_series(values, period)
    if len(series) < 2:
        return None
    return round(series[-1] - series[-2], 6)


def rsi(values: tuple[float, ...] | list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 6)


def roc(values: tuple[float, ...] | list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period + 1:
        return None
    prev = values[-1 - period]
    if prev == 0:
        return None
    return round((values[-1] / prev - 1.0) * 100.0, 6)


def atr(bars: tuple[Bar, ...], period: int) -> float | None:
    if period <= 0 or len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        high = bars[i].high
        low = bars[i].low
        prev_close = bars[i - 1].close
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    window = trs[-period:]
    return round(sum(window) / period, 6)


def rolling_std(values: tuple[float, ...] | list[float], period: int) -> float | None:
    if period <= 1 or len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    var = sum((v - mean) ** 2 for v in window) / period
    return round(var**0.5, 6)


def previous_high(bars: tuple[Bar, ...]) -> float | None:
    if len(bars) < 2:
        return None
    return bars[-2].high


def previous_low(bars: tuple[Bar, ...]) -> float | None:
    if len(bars) < 2:
        return None
    return bars[-2].low


def rolling_high(bars: tuple[Bar, ...], period: int, *, exclude_current: bool = True) -> float | None:
    source = bars[:-1] if exclude_current else bars
    if period <= 0 or len(source) < period:
        return None
    return max(bar.high for bar in source[-period:])


def rolling_low(bars: tuple[Bar, ...], period: int, *, exclude_current: bool = True) -> float | None:
    source = bars[:-1] if exclude_current else bars
    if period <= 0 or len(source) < period:
        return None
    return min(bar.low for bar in source[-period:])


def candle_range(bar: Bar) -> float:
    return round(bar.high - bar.low, 6)


def candle_body(bar: Bar) -> float:
    return round(bar.close - bar.open, 6)


def upper_wick(bar: Bar) -> float:
    return round(bar.high - max(bar.open, bar.close), 6)


def lower_wick(bar: Bar) -> float:
    return round(min(bar.open, bar.close) - bar.low, 6)
