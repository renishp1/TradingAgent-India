"""Deterministic synthetic index option chain. Not a market feed."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from grow.clock import IST
from grow.data.schema import SourceMeta
from grow.errors import GrowConfigError
from grow.options.models import (
    ExpiryClass,
    FieldSource,
    OptionChainSnapshot,
    OptionContract,
    OptionExpiry,
    OptionType,
)
from grow.options.source import OptionChainSource

FIXTURE_META = SourceMeta(
    name="grow.options.fixture.v1",
    vendor="none",
    license="synthetic-fixture",
    is_live=False,
    is_fixture=True,
    schema="grow.options.chain.v1",
)

_STEP = {"NIFTY": 50.0, "BANKNIFTY": 100.0}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FixtureOptionChain:
    """Weekly + monthly CE/PE around ATM. Includes liquid, illiquid, and bad quotes."""

    def meta(self) -> SourceMeta:
        return FIXTURE_META

    def snapshot(self, underlying: str, as_of: datetime, *, spot: float) -> OptionChainSnapshot:
        name = underlying.strip().upper()
        if name not in _STEP:
            raise GrowConfigError(f"{name} is not a 2C index")
        if as_of.tzinfo is None or getattr(as_of.tzinfo, "key", None) != "Asia/Kolkata":
            raise GrowConfigError("option chain as_of must be Asia/Kolkata")
        step = _STEP[name]
        atm = round(spot / step) * step
        day = as_of.astimezone(IST).date()
        weekly_near = _next_tuesday(day, inclusive=False)
        weekly_far = _next_tuesday(weekly_near + timedelta(days=1), inclusive=True)
        monthly = _monthly_expiry(day)
        expiries = (
            OptionExpiry(weekly_near, ExpiryClass.WEEKLY),
            OptionExpiry(weekly_far, ExpiryClass.WEEKLY),
            OptionExpiry(monthly, ExpiryClass.MONTHLY),
        )
        strikes = tuple(atm + step * offset for offset in range(-4, 5))
        contracts: list[OptionContract] = []
        for expiry in expiries:
            for strike in strikes:
                for option_type in (OptionType.CE, OptionType.PE):
                    contracts.append(
                        _contract(name, expiry, strike, option_type, atm=atm, spot=spot, as_of=as_of, step=step)
                    )
        # Intentional defects on the far weekly, far OTM CE — for filter tests.
        stamp = as_of
        contracts.append(
            OptionContract(
                underlying=name,
                expiry=weekly_far,
                expiry_class=ExpiryClass.WEEKLY,
                strike=atm + 8 * step,
                option_type=OptionType.CE,
                bid=12.0,
                ask=10.0,
                last_price=11.0,
                volume=5,
                open_interest=10,
                previous_open_interest=None,
                implied_volatility=0.12,
                delta=0.05,
                gamma=None,
                theta=None,
                vega=None,
                timestamp=stamp,
                provider_contract_id=f"{name}-CROSSED",
                iv_source=FieldSource.PROVIDER,
                greek_source=FieldSource.PROVIDER,
            )
        )
        snapshot_id = _digest(f"{name}:{as_of.isoformat()}:{spot}:{FIXTURE_META.name}")[:16]
        return OptionChainSnapshot(
            snapshot_id=snapshot_id,
            underlying=name,
            as_of=as_of,
            spot=spot,
            expiries=expiries,
            contracts=tuple(contracts),
            source_id=FIXTURE_META.name,
            is_fixture=True,
            provider_metadata={"synthetic": True, "atm": atm, "step": step},
        )


def _next_tuesday(day: date, *, inclusive: bool) -> date:
    probe = day if inclusive else day + timedelta(days=1)
    while probe.weekday() != 1:
        probe += timedelta(days=1)
    return probe


def _monthly_expiry(day: date) -> date:
    if day.month == 12:
        cursor = date(day.year + 1, 1, 1)
    else:
        cursor = date(day.year, day.month + 1, 1)
    last = date(cursor.year, cursor.month, 1) - timedelta(days=1)
    while last.weekday() != 1:
        last -= timedelta(days=1)
    if last <= day:
        return _monthly_expiry(last + timedelta(days=1))
    return last


def _contract(
    underlying: str,
    expiry: OptionExpiry,
    strike: float,
    option_type: OptionType,
    *,
    atm: float,
    spot: float,
    as_of: datetime,
    step: float,
) -> OptionContract:
    intrinsic = max(spot - strike, 0.0) if option_type is OptionType.CE else max(strike - spot, 0.0)
    distance = abs(strike - atm) / step
    digest = _digest(f"{underlying}:{expiry.day}:{strike}:{option_type.value}")
    noise = (int(digest[:4], 16) % 40) / 10.0
    mid = max(2.0, intrinsic + 18.0 + (4 - distance) * 6.0 + noise)
    wide = distance >= 3
    half = 8.0 if wide else 0.35
    bid = round(max(0.5, mid - half), 2)
    ask = round(mid + half, 2)
    volume = 40 if wide else 2500
    oi = 80 if wide else 18000
    has_iv = distance <= 2
    iv = round(0.12 + distance * 0.03, 4) if has_iv else None
    delta = None
    if has_iv:
        if option_type is OptionType.CE:
            delta = round(max(0.05, min(0.95, 0.50 - (strike - spot) / (8 * step))), 4)
        else:
            delta = round(min(-0.05, max(-0.95, -0.50 - (spot - strike) / (8 * step))), 4)
    return OptionContract(
        underlying=underlying,
        expiry=expiry.day,
        expiry_class=expiry.klass,
        strike=strike,
        option_type=option_type,
        bid=bid,
        ask=ask,
        last_price=round(mid, 2),
        volume=volume,
        open_interest=oi,
        previous_open_interest=None,
        implied_volatility=iv,
        delta=delta,
        gamma=None,
        theta=None,
        vega=None,
        timestamp=as_of,
        provider_contract_id=f"{underlying}{expiry.day:%d%b%y}{strike:.0f}{option_type.value}".upper(),
        iv_source=FieldSource.PROVIDER if has_iv else FieldSource.UNAVAILABLE,
        greek_source=FieldSource.PROVIDER if delta is not None else FieldSource.UNAVAILABLE,
    )


def open_option_source() -> OptionChainSource:
    return FixtureOptionChain()
