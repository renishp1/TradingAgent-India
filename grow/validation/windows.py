"""Chronological walk-forward window generation.

Random train/test splits are rejected. Windows roll forward in time with an
optional embargo between train, validation, and out-of-sample test periods.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from grow.errors import GrowConfigError


@dataclass(frozen=True)
class WalkWindow:
    """One chronological train | embargo | validate | embargo | test block."""

    train: tuple[date, date]
    validate: tuple[date, date]
    test: tuple[date, date]
    index: int

    def overlaps_test(self, other: "WalkWindow") -> bool:
        return not (self.test[1] < other.test[0] or other.test[1] < self.test[0])

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "train": [self.train[0].isoformat(), self.train[1].isoformat()],
            "validate": [self.validate[0].isoformat(), self.validate[1].isoformat()],
            "test": [self.test[0].isoformat(), self.test[1].isoformat()],
        }


def assert_chronological(sessions: tuple[date, ...]) -> None:
    if len(sessions) != len(set(sessions)):
        raise GrowConfigError("WALK_FORWARD_DUPLICATE_SESSIONS")
    if list(sessions) != sorted(sessions):
        raise GrowConfigError("WALK_FORWARD_NON_CHRONOLOGICAL")


def split_walk_windows(
    sessions: tuple[date, ...],
    *,
    train: int,
    validate: int,
    test: int,
    step: int,
    embargo: int,
) -> tuple[WalkWindow, ...]:
    """Build rolling chronological windows. Test never overlaps train/validate."""

    assert_chronological(sessions)
    if min(train, validate, test, step) < 1:
        raise GrowConfigError("WALK_FORWARD_INVALID_SIZES")
    if embargo < 0:
        raise GrowConfigError("WALK_FORWARD_NEGATIVE_EMBARGO")

    windows: list[WalkWindow] = []
    i = 0
    block = train + embargo + validate + embargo + test
    while i + block <= len(sessions):
        t0 = i
        t1 = i + train
        v0 = t1 + embargo
        v1 = v0 + validate
        s0 = v1 + embargo
        s1 = s0 + test
        window = WalkWindow(
            train=(sessions[t0], sessions[t1 - 1]),
            validate=(sessions[v0], sessions[v1 - 1]),
            test=(sessions[s0], sessions[s1 - 1]),
            index=len(windows),
        )
        if not (window.train[1] < window.validate[0] and window.validate[1] < window.test[0]):
            raise GrowConfigError("WALK_FORWARD_WINDOW_OVERLAP")
        windows.append(window)
        i += step
    return tuple(windows)


def sessions_in_range(sessions: tuple[date, ...], start: date, end: date) -> tuple[date, ...]:
    return tuple(day for day in sessions if start <= day <= end)
