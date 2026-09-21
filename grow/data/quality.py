"""Freshness and missing-candle checks."""

from __future__ import annotations

from datetime import datetime

from grow.clock import IST
from grow.data.schema import BarSeries, SnapshotQuality, Timeframe
from grow.data.schedule import complete_starts


def assess_series(
    series: BarSeries,
    *,
    session_day,
    as_of: datetime,
    session_open,
    session_close,
    stale_after_seconds: int,
) -> SnapshotQuality:
    as_of = as_of.astimezone(IST)
    if series.timeframe is Timeframe.D1:
        last = series.bars[-1] if series.bars else None
        session_end = datetime.combine(session_day, session_close, tzinfo=IST)
        forming = last is not None and last.end < session_end
        last_end = None if last is None else last.end
        lag = (as_of - last_end).total_seconds() if last_end is not None else 10**9
        notes = (
            ("forming D1: only data available up to as_of",)
            if forming
            else ("D1 complete through session close",)
        )
        return SnapshotQuality(
            complete=last is not None and not forming,
            stale=lag > stale_after_seconds,
            missing_count=0,
            expected_count=len(series.bars),
            last_bar_end=last_end,
            notes=notes,
        )
    expected = complete_starts(
        session_day,
        series.timeframe,
        as_of,
        session_open=session_open,
        session_close=session_close,
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

