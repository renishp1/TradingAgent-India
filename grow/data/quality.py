"""Freshness and missing-candle checks."""

from __future__ import annotations

from datetime import datetime

from grow.data.schema import BarSeries, SnapshotQuality, Timeframe
from grow.data.schedule import bar_duration, complete_starts


def assess_series(
    series: BarSeries,
    *,
    session_day,
    as_of: datetime,
    session_open,
    session_close,
    stale_after_seconds: int,
) -> SnapshotQuality:
    expected = complete_starts(
        session_day,
        series.timeframe,
        as_of,
        session_open=session_open,
        session_close=session_close,
    )
    if series.timeframe is Timeframe.D1:
        last_end = series.bars[-1].end if series.bars else None
        lag = (as_of - last_end).total_seconds() if last_end is not None else 10**9
        return SnapshotQuality(
            complete=len(series.bars) > 0,
            stale=lag > stale_after_seconds,
            missing_count=0,
            expected_count=len(series.bars),
            last_bar_end=last_end,
            notes=("daily bars are session aggregates; current day may still be forming",),
        )
    have = {bar.start for bar in series.bars}
    missing = tuple(start for start in expected if start not in have)
    last_end = series.bars[-1].end if series.bars else None
    stale = False
    notes: list[str] = []
    if last_end is None:
        notes.append("no bars")
        stale = True
    else:
        lag = (as_of - last_end).total_seconds()
        stale = lag > stale_after_seconds
        if stale:
            notes.append(f"lag={int(lag)}s")
        if series.timeframe is Timeframe.D1 and not missing:
            notes.append("daily series uses completed sessions only")
    return SnapshotQuality(
        complete=len(missing) == 0 and bool(expected),
        stale=stale,
        missing_count=len(missing),
        expected_count=len(expected),
        last_bar_end=last_end,
        notes=tuple(notes),
    )


def combine_quality(parts: tuple[SnapshotQuality, ...]) -> SnapshotQuality:
    if not parts:
        return SnapshotQuality(
            complete=False,
            stale=True,
            missing_count=0,
            expected_count=0,
            last_bar_end=None,
            notes=("no series",),
        )
    last_ends = [p.last_bar_end for p in parts if p.last_bar_end is not None]
    notes: list[str] = []
    for part in parts:
        notes.extend(part.notes)
    return SnapshotQuality(
        complete=all(p.complete for p in parts),
        stale=any(p.stale for p in parts),
        missing_count=sum(p.missing_count for p in parts),
        expected_count=sum(p.expected_count for p in parts),
        last_bar_end=max(last_ends) if last_ends else None,
        notes=tuple(notes),
    )


def duration_seconds(timeframe: Timeframe) -> float:
    return bar_duration(timeframe).total_seconds()
