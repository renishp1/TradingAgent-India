"""Versioned index-option universe. OPTIDX only. Not stock options.

NIFTY remains WEEKLY_PREFERRED. BANKNIFTY and MIDCPNIFTY are MONTHLY_ONLY.
Additional approved index underlyings are added here, not by hard-coding the
provider layer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import hashlib
import json

from grow.clock import IST
from grow.errors import GrowConfigError

WEEKLY_PREFERRED = "WEEKLY_PREFERRED"
MONTHLY_ONLY = "MONTHLY_ONLY"
WEEKLY_THEN_MONTHLY = "WEEKLY_THEN_MONTHLY"
CUSTOM_HISTORICAL = "CUSTOM_HISTORICAL"
KNOWN_EXPIRY_PROFILES = frozenset(
    {WEEKLY_PREFERRED, MONTHLY_ONLY, WEEKLY_THEN_MONTHLY, CUSTOM_HISTORICAL}
)
OPTIDX = "OPTIDX"
REGISTRY_VERSION = "index.universe.v1"
KNOWN_STRIKE_PROFILES = frozenset({"ATM_PM0", "ATM_PM1", "ATM_PM2", "ATM_PM3"})
KNOWN_LIQUIDITY_POLICIES = frozenset({"options.select.v1"})
KNOWN_LOT_SIZE_SOURCES = frozenset({"CONTRACT_MASTER"})
FORBIDDEN_UNDERLYINGS = frozenset(
    {
        "RELIANCE",
        "TCS",
        "HDFCBANK",
        "INFY",
        "ICICIBANK",
        "SBIN",
        "BHARTIARTL",
        "ITC",
        "LT",
        "HINDUNILVR",
        "FUT",
        "FUTURES",
    }
)


@dataclass(frozen=True)
class IndexPolicy:
    index_id: str
    canonical_symbol: str
    display_name: str
    exchange: str
    instrument_type: str
    active_from: date
    active_to: date | None
    option_supported: bool
    expiry_policy_profile: str
    strike_policy_profile: str
    lot_size_source: str
    liquidity_policy: str
    research_status: str
    license_status: str
    provider_symbol_map: tuple[tuple[str, str], ...] = ()

    def active_on(self, day: date) -> bool:
        if day < self.active_from:
            return False
        if self.active_to is not None and day > self.active_to:
            return False
        return True


@dataclass(frozen=True)
class EligibleUnderlying:
    index_id: str
    canonical_symbol: str
    expiry_policy_profile: str
    status: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "index_id": self.index_id,
            "canonical_symbol": self.canonical_symbol,
            "expiry_policy_profile": self.expiry_policy_profile,
            "status": self.status,
            "reason": self.reason,
        }


def _policy(
    symbol: str,
    *,
    name: str,
    profile: str,
    active_from: date,
    active_to: date | None = None,
    exchange: str = "NSE",
) -> IndexPolicy:
    return IndexPolicy(
        index_id=symbol,
        canonical_symbol=symbol,
        display_name=name,
        exchange=exchange,
        instrument_type=OPTIDX,
        active_from=active_from,
        active_to=active_to,
        option_supported=True,
        expiry_policy_profile=profile,
        strike_policy_profile="ATM_PM2",
        lot_size_source="CONTRACT_MASTER",
        liquidity_policy="options.select.v1",
        research_status="APPROVED",
        license_status="POLICY",
        provider_symbol_map=(("canonical", symbol),),
    )


def default_index_policies() -> tuple[IndexPolicy, ...]:
    return (
        _policy("NIFTY", name="Nifty 50", profile=WEEKLY_PREFERRED, active_from=date(2019, 1, 1)),
        _policy("BANKNIFTY", name="Nifty Bank", profile=MONTHLY_ONLY, active_from=date(2019, 1, 1)),
        _policy("MIDCPNIFTY", name="Nifty Midcap Select", profile=MONTHLY_ONLY, active_from=date(2023, 1, 1)),
        _policy(
            "SENSEX",
            name="S&P BSE Sensex",
            profile=WEEKLY_PREFERRED,
            active_from=date(2023, 1, 1),
            exchange="BSE",
        ),
    )


class IndexUniverseRegistry:
    def __init__(self, policies: tuple[IndexPolicy, ...] | None = None) -> None:
        rows = policies or default_index_policies()
        by_id: dict[str, IndexPolicy] = {}
        for policy in rows:
            if policy.instrument_type != OPTIDX:
                raise GrowConfigError(f"UNSUPPORTED_INSTRUMENT_TYPE:{policy.instrument_type}")
            if policy.expiry_policy_profile not in KNOWN_EXPIRY_PROFILES:
                raise GrowConfigError(f"UNKNOWN_EXPIRY_PROFILE:{policy.expiry_policy_profile}")
            if policy.strike_policy_profile not in KNOWN_STRIKE_PROFILES:
                raise GrowConfigError(f"UNKNOWN_STRIKE_PROFILE:{policy.strike_policy_profile}")
            if policy.liquidity_policy not in KNOWN_LIQUIDITY_POLICIES:
                raise GrowConfigError(f"UNKNOWN_LIQUIDITY_POLICY:{policy.liquidity_policy}")
            if policy.lot_size_source not in KNOWN_LOT_SIZE_SOURCES:
                raise GrowConfigError(f"UNKNOWN_LOT_SIZE_SOURCE:{policy.lot_size_source}")
            by_id[policy.canonical_symbol] = policy
        self._policies = by_id
        self.version = REGISTRY_VERSION
        self.fingerprint = fingerprint_policies(tuple(by_id.values()))

    def policy(self, symbol: str, day: date | None = None) -> IndexPolicy | None:
        item = self._policies.get(symbol)
        if item is None:
            return None
        if day is not None and not item.active_on(day):
            return None
        return item

    def allows(self, symbol: str, day: date | None = None) -> bool:
        item = self.policy(symbol, day)
        return bool(item and item.option_supported)

    def symbols(self, day: date | None = None) -> frozenset[str]:
        return frozenset(s for s, p in self._policies.items() if day is None or p.active_on(day))

    def all_policies(self) -> tuple[IndexPolicy, ...]:
        return tuple(self._policies.values())


_DEFAULT: IndexUniverseRegistry | None = None


def default_index_registry() -> IndexUniverseRegistry:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = IndexUniverseRegistry()
    return _DEFAULT


def fingerprint_policies(policies: tuple[IndexPolicy, ...]) -> str:
    rows = []
    for policy in sorted(policies, key=lambda item: item.canonical_symbol):
        rows.append(
            {
                "index_id": policy.index_id,
                "symbol": policy.canonical_symbol,
                "expiry": policy.expiry_policy_profile,
                "strike": policy.strike_policy_profile,
                "lot": policy.lot_size_source,
                "liquidity": policy.liquidity_policy,
                "from": policy.active_from.isoformat(),
                "to": None if policy.active_to is None else policy.active_to.isoformat(),
                "option_supported": policy.option_supported,
            }
        )
    body = json.dumps({"version": REGISTRY_VERSION, "policies": rows}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def strike_window_distance(profile: str) -> int:
    if profile not in KNOWN_STRIKE_PROFILES or not profile.startswith("ATM_PM"):
        raise GrowConfigError(f"UNKNOWN_STRIKE_PROFILE:{profile}")
    return int(profile.removeprefix("ATM_PM"))


def apply_index_policy(options_config, policy: IndexPolicy):
    """Map IndexPolicy research profiles onto 2C options config. Fail closed.

    v1: strike_policy_profile sets ATM window. liquidity_policy is provenance
    only — options.select.v1 does not change 2C min volume/OI/spread gates.
    lot_size_source=CONTRACT_MASTER is enforced after 2C selection.
    """
    if policy.liquidity_policy not in KNOWN_LIQUIDITY_POLICIES:
        raise GrowConfigError(f"UNKNOWN_LIQUIDITY_POLICY:{policy.liquidity_policy}")
    if policy.lot_size_source not in KNOWN_LOT_SIZE_SOURCES:
        raise GrowConfigError(f"UNKNOWN_LOT_SIZE_SOURCE:{policy.lot_size_source}")
    distance = strike_window_distance(policy.strike_policy_profile)
    return replace(options_config, max_distance_from_atm=distance)


def is_forbidden_instrument(symbol: str) -> bool:
    name = symbol.upper()
    if name in FORBIDDEN_UNDERLYINGS:
        return True
    if name.startswith("FUT") or name.endswith("FUT"):
        return True
    return False


def is_supported_index(symbol: str, day: date | None = None) -> bool:
    if is_forbidden_instrument(symbol):
        return False
    return default_index_registry().allows(symbol, day)


def discover_underlyings(store, as_of: datetime, registry: IndexUniverseRegistry | None = None) -> tuple[EligibleUnderlying, ...]:
    """Contracts are the listed universe. Registry is the approval overlay."""
    reg = registry or default_index_registry()
    moment = as_of.astimezone(IST)
    day = moment.date()
    listed: set[str] = set()
    for contract in store.all_contracts():
        if contract.first_seen_at <= moment <= contract.last_seen_at:
            listed.add(contract.underlying)
    rows: list[EligibleUnderlying] = []
    seen: set[str] = set()
    for symbol in sorted(listed):
        if is_forbidden_instrument(symbol):
            rows.append(
                EligibleUnderlying(symbol, symbol, "", "UNAUTHORIZED", "FORBIDDEN_INSTRUMENT")
            )
            continue
        pol = reg.policy(symbol, day)
        if pol is None or not pol.option_supported:
            rows.append(
                EligibleUnderlying(symbol, symbol, "", "UNAUTHORIZED", "NOT_APPROVED")
            )
            continue
        rows.append(
            EligibleUnderlying(
                index_id=pol.index_id,
                canonical_symbol=pol.canonical_symbol,
                expiry_policy_profile=pol.expiry_policy_profile,
                status="ELIGIBLE",
                reason="LISTED",
            )
        )
        seen.add(symbol)
    for policy in sorted(reg.all_policies(), key=lambda p: p.canonical_symbol):
        if policy.canonical_symbol in seen:
            continue
        if not policy.active_on(day) or not policy.option_supported:
            continue
        rows.append(
            EligibleUnderlying(
                index_id=policy.index_id,
                canonical_symbol=policy.canonical_symbol,
                expiry_policy_profile=policy.expiry_policy_profile,
                status="DATA_UNAVAILABLE",
                reason="DATA_UNAVAILABLE",
            )
        )
    return tuple(rows)


def eligible_symbols(discovered: tuple[EligibleUnderlying, ...]) -> tuple[str, ...]:
    return tuple(row.canonical_symbol for row in discovered if row.status == "ELIGIBLE")
