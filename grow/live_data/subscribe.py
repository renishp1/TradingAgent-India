"""Deterministic live subscription planner. Does not change 2C selection rules."""

from __future__ import annotations

from datetime import date
from typing import Iterable, Mapping, Sequence

from grow.errors import GrowConfigError
from grow.live_data.symbols import ParsedInstrument


def strike_step(strikes: Sequence[float]) -> float:
    unique = sorted({float(s) for s in strikes})
    if len(unique) < 2:
        return 50.0
    diffs = [unique[i + 1] - unique[i] for i in range(len(unique) - 1)]
    diffs.sort()
    return diffs[len(diffs) // 2]


def atm_strike(spot: float, step: float) -> float:
    if step <= 0:
        raise GrowConfigError("INVALID_STRIKE")
    return round(spot / step) * step


def plan_subscriptions(
    *,
    instruments: Sequence[ParsedInstrument | Mapping[str, object]],
    spots: Mapping[str, float],
    selected_expiry: Mapping[str, date],
    strike_window: int,
    max_symbols: int,
    active_underlyings: Sequence[str],
) -> tuple[str, ...]:
    """Index spots + ATM±window CE/PE for the selected expiry. Cap by dropping farthest OTM first."""
    if max_symbols < 1:
        raise GrowConfigError("SUBSCRIPTION_LIMIT")
    catalog: list[tuple[str, str, date | None, float | None, str | None]] = []
    for item in instruments:
        if isinstance(item, ParsedInstrument):
            catalog.append((item.provider_symbol, item.canonical_symbol, item.expiry, item.strike, item.option_type))
        else:
            catalog.append(
                (
                    str(item["provider_symbol"]),
                    str(item["canonical_symbol"]),
                    item.get("expiry"),  # type: ignore[arg-type]
                    None if item.get("strike") is None else float(item["strike"]),  # type: ignore[arg-type]
                    None if item.get("option_type") is None else str(item["option_type"]),
                )
            )
    desired: list[tuple[int, float, str]] = []
    used: set[str] = set()
    for underlying in active_underlyings:
        expiry = selected_expiry.get(underlying)
        spot = spots.get(underlying)
        options = [row for row in catalog if row[1] == underlying and row[4] in {"CE", "PE"} and row[2] == expiry]
        index_rows = [row for row in catalog if row[1] == underlying and row[4] is None]
        if index_rows:
            sym = index_rows[0][0]
            if sym not in used:
                desired.append((0, 0.0, sym))
                used.add(sym)
        if not options or spot is None or expiry is None:
            continue
        step = strike_step([float(row[3]) for row in options if row[3] is not None])
        atm = atm_strike(spot, step)
        for provider_symbol, _und, _exp, strike, _kind in options:
            if strike is None or provider_symbol in used:
                continue
            distance = abs(strike - atm) / step
            if distance > strike_window:
                continue
            desired.append((1, distance, provider_symbol))
            used.add(provider_symbol)
    desired.sort(key=lambda row: (row[0], row[1], row[2]))
    if len(desired) > max_symbols:
        keep_index = [row for row in desired if row[0] == 0]
        options = [row for row in desired if row[0] == 1]
        room = max(0, max_symbols - len(keep_index))
        desired = keep_index + options[:room]
    if not desired:
        return ()
    if len(desired) > max_symbols:
        raise GrowConfigError("SUBSCRIPTION_LIMIT")
    return tuple(row[2] for row in desired)


def resubscribe_set(previous: Iterable[str], planned: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    prev = tuple(previous)
    plan = tuple(planned)
    add = tuple(s for s in plan if s not in prev)
    drop = tuple(s for s in prev if s not in plan)
    return add, drop
