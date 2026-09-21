"""Expiry, ATM, strike window. Never invent strikes or expiries."""

from __future__ import annotations

from datetime import date, datetime

from grow.clock import IST
from grow.config import OptionsConfig
from grow.options.models import ExpiryClass, OptionChainSnapshot, OptionContract, OptionExpiry, OptionType
from grow.options.validate import moneyness as classify_moneyness


def session_day(as_of: datetime) -> date:
    return as_of.astimezone(IST).date()


def strike_step(strikes: tuple[float, ...]) -> float | None:
    uniq = sorted(set(strikes))
    diffs = [round(uniq[i + 1] - uniq[i], 6) for i in range(len(uniq) - 1) if uniq[i + 1] > uniq[i]]
    if not diffs:
        return None
    counts: dict[float, int] = {}
    for diff in diffs:
        counts[diff] = counts.get(diff, 0) + 1
    return max(counts, key=lambda key: (counts[key], -key))


def atm_strike(spot: float, strikes: tuple[float, ...]) -> float | None:
    if not strikes:
        return None
    return min(strikes, key=lambda strike: (abs(strike - spot), strike))


def strike_window(spot: float, strikes: tuple[float, ...], distance: int) -> tuple[float, ...]:
    uniq = tuple(sorted(set(strikes)))
    atm = atm_strike(spot, uniq)
    if atm is None:
        return ()
    index = uniq.index(atm)
    lo = max(0, index - distance)
    hi = min(len(uniq), index + distance + 1)
    return uniq[lo:hi]


def choose_expiry(
    chain: OptionChainSnapshot,
    as_of: datetime,
    config: OptionsConfig,
) -> tuple[OptionExpiry | None, str]:
    today = session_day(as_of)
    preferred = ExpiryClass.WEEKLY if config.preferred_expiry_class == "weekly" else ExpiryClass.MONTHLY
    eligible: list[OptionExpiry] = []
    for expiry in chain.expiries:
        if expiry.day < today:
            continue
        if expiry.day == today and not config.allow_same_day:
            continue
        if expiry.klass is not preferred:
            continue
        eligible.append(expiry)
    if not eligible:
        return None, f"NO_ELIGIBLE_EXPIRY:{preferred.value}"
    chosen = min(eligible, key=lambda item: (item.day, item.klass.value))
    return chosen, f"EXPIRY:{chosen.day.isoformat()}:{chosen.klass.value}"


def allowed_type(direction: str) -> OptionType | None:
    if direction == "BULLISH":
        return OptionType.CE
    if direction == "BEARISH":
        return OptionType.PE
    return None


def filter_universe(
    contracts: tuple[OptionContract, ...],
    *,
    expiry: OptionExpiry,
    option_type: OptionType,
    spot: float,
    window: tuple[float, ...],
    allowed_moneyness: tuple[str, ...],
) -> tuple[OptionContract, ...]:
    atm = atm_strike(spot, window)
    kept = []
    for contract in contracts:
        if contract.expiry != expiry.day:
            continue
        if contract.option_type is not option_type:
            continue
        if contract.strike not in window:
            continue
        kind = classify_moneyness(option_type, spot, contract.strike, atm or contract.strike)
        if kind not in allowed_moneyness:
            continue
        kept.append(contract)
    return tuple(kept)
