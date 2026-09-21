"""Vendor-row → Bar. Fail closed on a malformed candle."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from grow.data.schema import Bar, Timeframe
from grow.data.schedule import bar_duration
from grow.errors import GrowConfigError
from grow.types import Symbol


def _num(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise GrowConfigError(f"bar.{field} is not a number") from exc
    if number != number:  # NaN
        raise GrowConfigError(f"bar.{field} is NaN")
    return number


def normalize_bar(row: Mapping[str, Any], *, symbol: Symbol, timeframe: Timeframe) -> Bar:
    start = row.get("start")
    if not isinstance(start, datetime):
        raise GrowConfigError("bar.start must be a datetime")
    end = row.get("end")
    if end is None:
        end = start + bar_duration(timeframe)
    if not isinstance(end, datetime):
        raise GrowConfigError("bar.end must be a datetime")
    volume_raw = row.get("volume", 0)
    try:
        volume = int(volume_raw)
    except (TypeError, ValueError) as exc:
        raise GrowConfigError("bar.volume must be an integer") from exc
    try:
        return Bar(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            open=_num(row.get("open"), "open"),
            high=_num(row.get("high"), "high"),
            low=_num(row.get("low"), "low"),
            close=_num(row.get("close"), "close"),
            volume=volume,
        )
    except ValueError as exc:
        raise GrowConfigError(str(exc)) from exc
