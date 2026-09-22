"""Nearest eligible expiry resolver. Versioned policy profiles. No invented contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime

from grow.clock import IST
from grow.errors import GrowConfigError
from grow.history.models import HistoricalExpiryRecord
from grow.history.universe import CUSTOM_HISTORICAL, MONTHLY_ONLY, REGISTRY_VERSION, WEEKLY_PREFERRED, WEEKLY_THEN_MONTHLY

RESOLVER_VERSION = "expiry.resolver.v1"
NO_ELIGIBLE_EXPIRY = "NO_ELIGIBLE_EXPIRY"
EXPIRED = "EXPIRED"
SAME_DAY_FORBIDDEN = "SAME_DAY_FORBIDDEN"
NOT_LISTED_YET = "NOT_LISTED_YET"
DISALLOWED_CLASS = "DISALLOWED_CLASS"
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


@dataclass(frozen=True)
class ExpiryResolution:
    resolution_id: str
    underlying: str
    as_of: str
    discovered_expiries: tuple[str, ...]
    excluded_expiries: tuple[str, ...]
    policy_profile: str
    selected_expiry: str | None
    selected_expiry_class: str | None
    exclusion_reasons: tuple[str, ...]
    dataset_id: str
    dataset_version: str
    dataset_fingerprint: str
    resolver_version: str = RESOLVER_VERSION
    policy_registry_version: str = REGISTRY_VERSION
    policy_fingerprint: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "resolution_id": self.resolution_id,
            "underlying": self.underlying,
            "as_of": self.as_of,
            "discovered_expiries": list(self.discovered_expiries),
            "excluded_expiries": list(self.excluded_expiries),
            "policy_profile": self.policy_profile,
            "selected_expiry": self.selected_expiry,
            "selected_expiry_class": self.selected_expiry_class,
            "exclusion_reasons": list(self.exclusion_reasons),
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_fingerprint": self.dataset_fingerprint,
            "resolver_version": self.resolver_version,
            "policy_registry_version": self.policy_registry_version,
            "policy_fingerprint": self.policy_fingerprint,
        }


def _classes_for(profile: str) -> tuple[str, ...]:
    if profile == MONTHLY_ONLY:
        return ("MONTHLY",)
    if profile == WEEKLY_THEN_MONTHLY:
        return ("WEEKLY", "MONTHLY")
    if profile == WEEKLY_PREFERRED:
        return ("WEEKLY",)
    if profile == CUSTOM_HISTORICAL:
        raise GrowConfigError("CUSTOM_HISTORICAL_UNIMPLEMENTED")
    raise GrowConfigError(f"UNKNOWN_EXPIRY_PROFILE:{profile}")


def resolve_nearest_expiry(
    underlying: str,
    as_of: datetime,
    records: tuple[HistoricalExpiryRecord, ...],
    policy_profile: str,
    *,
    allow_same_day: bool = False,
    dataset_id: str = "",
    dataset_version: str = "",
    dataset_fingerprint: str = "",
    policy_registry_version: str = REGISTRY_VERSION,
    policy_fingerprint: str = "",
) -> ExpiryResolution:
    moment = as_of.astimezone(IST)
    today = moment.date()
    discovered = tuple(sorted(f"{r.expiry.isoformat()}:{r.expiry_class}" for r in records if r.underlying == underlying))
    preferred = _classes_for(policy_profile)
    excluded: list[str] = []
    reasons: list[str] = []
    by_class: dict[str, list[HistoricalExpiryRecord]] = {klass: [] for klass in preferred}
    for rec in records:
        if rec.underlying != underlying:
            continue
        label = f"{rec.expiry.isoformat()}:{rec.expiry_class}"
        if rec.first_seen_at > moment:
            excluded.append(label)
            reasons.append(f"{label}:{NOT_LISTED_YET}")
            continue
        if rec.last_seen_at < moment:
            excluded.append(label)
            reasons.append(f"{label}:{DATA_UNAVAILABLE}")
            continue
        if rec.expiry < today:
            excluded.append(label)
            reasons.append(f"{label}:{EXPIRED}")
            continue
        if rec.expiry == today and not allow_same_day:
            excluded.append(label)
            reasons.append(f"{label}:{SAME_DAY_FORBIDDEN}")
            continue
        if rec.expiry_class not in preferred:
            excluded.append(label)
            reasons.append(f"{label}:{DISALLOWED_CLASS}")
            continue
        by_class[rec.expiry_class].append(rec)
    selected = None
    selected_class = None
    for klass in preferred:
        pool = by_class.get(klass) or []
        if pool:
            chosen = min(pool, key=lambda rec: (rec.expiry, rec.expiry_class))
            selected = chosen.expiry.isoformat()
            selected_class = chosen.expiry_class
            break
    if selected is None:
        reasons.append(NO_ELIGIBLE_EXPIRY)
    payload = {
        "underlying": underlying,
        "as_of": moment.isoformat(),
        "profile": policy_profile,
        "selected": selected,
        "discovered": list(discovered),
        "version": RESOLVER_VERSION,
        "dataset_fingerprint": dataset_fingerprint,
        "policy_fingerprint": policy_fingerprint,
        "policy_registry_version": policy_registry_version,
    }
    rid = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ExpiryResolution(
        resolution_id=rid,
        underlying=underlying,
        as_of=moment.isoformat(),
        discovered_expiries=discovered,
        excluded_expiries=tuple(excluded),
        policy_profile=policy_profile,
        selected_expiry=selected,
        selected_expiry_class=selected_class,
        exclusion_reasons=tuple(reasons),
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        dataset_fingerprint=dataset_fingerprint,
        policy_registry_version=policy_registry_version,
        policy_fingerprint=policy_fingerprint,
    )
