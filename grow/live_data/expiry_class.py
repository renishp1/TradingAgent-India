"""Deterministic index-option expiry classification. Never infers WEEKLY from option-ness.

Provider metadata wins when it is WEEKLY/MONTHLY and agrees with the versioned
calendar. Calendar classifies a date only when it is a scheduled weekly or
monthly expiry under an explicit per-underlying weekday policy. Anything else
stays UNKNOWN and is ineligible for live subscription.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from grow.clock import IST, Clock, FrozenClock
from grow.live_data.catalog import KNOWN_EXPIRY_CLASSES, UNKNOWN_EXPIRY_CLASS
from grow.market.session import CALENDAR_VERSION as SESSION_CALENDAR_VERSION
from grow.market.session import CASH_HOLIDAYS_2026

POLICY_VERSION = "expiry.class.nse.v1"
CALENDAR_VERSION = SESSION_CALENDAR_VERSION
PROVIDER = "PROVIDER"
CALENDAR = "CALENDAR"
NONE = "NONE"

RULE_PROVIDER = "expiry.class.provider.v1"
RULE_CALENDAR_WEEKLY = "expiry.class.calendar.weekly.v1"
RULE_CALENDAR_MONTHLY = "expiry.class.calendar.monthly.v1"
RULE_CONFLICT = "expiry.class.conflict.v1"
RULE_UNSUPPORTED = "expiry.class.unsupported.v1"
RULE_UNKNOWN = "expiry.class.unknown.v1"
RULE_NOT_READY = "expiry.class.not_ready.v1"

CLASSIFICATION_CONFLICT = "CLASSIFICATION_CONFLICT"
CLASSIFIER_NOT_READY = "CLASSIFIER_NOT_READY"
PROVIDER_CLASS_UNSUPPORTED = "PROVIDER_CLASS_UNSUPPORTED"


@dataclass(frozen=True)
class ExpirySchedule:
    """Per-underlying weekday rules. Not a hard-coded trading universe."""

    canonical_symbol: str
    weekly_weekday: int | None
    monthly_weekday: int | None
    active_from: date
    active_to: date | None = None

    def active_on(self, day: date) -> bool:
        if day < self.active_from:
            return False
        if self.active_to is not None and day > self.active_to:
            return False
        return True


@dataclass(frozen=True)
class ExpiryClassification:
    expiry_class: str
    rule_id: str
    policy_version: str
    evidence_source: str
    evidence_fingerprint: str | None
    classified_at: datetime
    valid_for_date: date
    diagnostic: str | None
    calendar_version: str = CALENDAR_VERSION
    provider_symbol: str = ""
    canonical_symbol: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "expiry_class": self.expiry_class,
            "rule_id": self.rule_id,
            "policy_version": self.policy_version,
            "evidence_source": self.evidence_source,
            "evidence_fingerprint": self.evidence_fingerprint,
            "classified_at": self.classified_at.isoformat(),
            "valid_for_date": self.valid_for_date.isoformat(),
            "diagnostic": self.diagnostic,
            "calendar_version": self.calendar_version,
            "provider_symbol": self.provider_symbol,
            "canonical_symbol": self.canonical_symbol,
        }


def default_expiry_schedules() -> tuple[ExpirySchedule, ...]:
    """Versioned weekday policy. Matches 2I sample dates; not an allow-list.

    Weekly = weekday of the weekly contract. Monthly = last such weekday of
    the month, holiday-adjusted to the previous session. When weekly and
    monthly share a weekday, the last occurrence is MONTHLY.
    """
    start = date(2019, 1, 1)
    return (
        ExpirySchedule("NIFTY", 1, 3, start),
        ExpirySchedule("BANKNIFTY", 1, 3, start),
        ExpirySchedule("MIDCPNIFTY", None, 1, date(2023, 1, 1)),
        ExpirySchedule("FINNIFTY", 1, 1, date(2021, 1, 1)),
    )


def is_session_day(day: date, holidays: Iterable[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def previous_session_day(day: date, holidays: Iterable[date]) -> date | None:
    cursor = day
    holiday_set = frozenset(holidays)
    for _ in range(14):
        if is_session_day(cursor, holiday_set):
            return cursor
        cursor -= timedelta(days=1)
    return None


def last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def _fingerprint(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ExpiryClassifier:
    def __init__(
        self,
        *,
        schedules: Sequence[ExpirySchedule] | None = None,
        holidays: Iterable[date] | None = None,
        clock: Clock | None = None,
        policy_version: str = POLICY_VERSION,
        calendar_version: str = CALENDAR_VERSION,
    ) -> None:
        self.schedules = tuple(schedules if schedules is not None else default_expiry_schedules())
        self.holidays = frozenset(holidays if holidays is not None else CASH_HOLIDAYS_2026)
        self.clock = clock or FrozenClock(datetime.now(tz=IST))
        self.policy_version = policy_version
        self.calendar_version = calendar_version
        self._cache: dict[tuple[Any, ...], ExpiryClassification] = {}

    def schedule_for(self, underlying: str, day: date) -> ExpirySchedule | None:
        name = str(underlying or "").upper()
        for row in self.schedules:
            if row.canonical_symbol == name and row.active_on(day):
                return row
        return None

    def invalidate(self) -> None:
        self._cache.clear()

    def classify(
        self,
        *,
        provider_symbol: str,
        canonical_symbol: str,
        expiry: date | None,
        option_type: str | None,
        provider_class: Any = None,
        as_of: datetime | date | None = None,
    ) -> ExpiryClassification:
        moment = self._as_of(as_of)
        valid_for = moment.date() if isinstance(moment, datetime) else moment
        cache_key = (
            str(provider_symbol),
            None if expiry is None else expiry.isoformat(),
            None if provider_class in (None, "") else str(provider_class).strip().upper(),
            self.policy_version,
            self.calendar_version,
            frozenset(self.holidays),
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        result = self._classify(
            provider_symbol=str(provider_symbol),
            canonical_symbol=str(canonical_symbol).upper(),
            expiry=expiry,
            option_type=None if option_type in (None, "") else str(option_type).upper(),
            provider_class=provider_class,
            classified_at=moment if isinstance(moment, datetime) else datetime.combine(moment, datetime.min.time(), tzinfo=IST),
            valid_for=valid_for,
        )
        self._cache[cache_key] = result
        return result

    def _as_of(self, as_of: datetime | date | None) -> datetime:
        if as_of is None:
            return self.clock.now()
        if isinstance(as_of, datetime):
            return as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=IST)
        return datetime(as_of.year, as_of.month, as_of.day, 11, 0, tzinfo=IST)

    def _classify(
        self,
        *,
        provider_symbol: str,
        canonical_symbol: str,
        expiry: date | None,
        option_type: str | None,
        provider_class: Any,
        classified_at: datetime,
        valid_for: date,
    ) -> ExpiryClassification:
        if option_type not in {"CE", "PE"} or expiry is None:
            return self._result(
                UNKNOWN_EXPIRY_CLASS,
                RULE_UNKNOWN,
                NONE,
                classified_at,
                valid_for,
                "NOT_AN_OPTION" if option_type not in {"CE", "PE"} else "MISSING_EXPIRY",
                provider_symbol,
                canonical_symbol,
                provider_class,
                None,
            )
        schedule = self.schedule_for(canonical_symbol, valid_for)
        calendar_class = None if schedule is None else self._calendar_class(schedule, expiry)
        raw = None if provider_class in (None, "") else str(provider_class).strip().upper()
        provider_valid = raw in KNOWN_EXPIRY_CLASSES
        provider_malformed = raw is not None and not provider_valid

        if schedule is None:
            return self._result(
                UNKNOWN_EXPIRY_CLASS,
                RULE_NOT_READY,
                NONE,
                classified_at,
                valid_for,
                CLASSIFIER_NOT_READY,
                provider_symbol,
                canonical_symbol,
                raw,
                calendar_class,
            )
        if provider_valid and calendar_class is not None and provider_valid and raw != calendar_class:
            return self._result(
                UNKNOWN_EXPIRY_CLASS,
                RULE_CONFLICT,
                NONE,
                classified_at,
                valid_for,
                CLASSIFICATION_CONFLICT,
                provider_symbol,
                canonical_symbol,
                raw,
                calendar_class,
            )
        if provider_valid:
            return self._result(
                raw or UNKNOWN_EXPIRY_CLASS,
                RULE_PROVIDER,
                PROVIDER,
                classified_at,
                valid_for,
                None,
                provider_symbol,
                canonical_symbol,
                raw,
                calendar_class,
            )
        if calendar_class == "WEEKLY":
            return self._result(
                "WEEKLY",
                RULE_CALENDAR_WEEKLY,
                CALENDAR,
                classified_at,
                valid_for,
                None,
                provider_symbol,
                canonical_symbol,
                raw,
                calendar_class,
            )
        if calendar_class == "MONTHLY":
            return self._result(
                "MONTHLY",
                RULE_CALENDAR_MONTHLY,
                CALENDAR,
                classified_at,
                valid_for,
                None,
                provider_symbol,
                canonical_symbol,
                raw,
                calendar_class,
            )
        diagnostic = PROVIDER_CLASS_UNSUPPORTED if provider_malformed else UNKNOWN_EXPIRY_CLASS
        rule = RULE_UNSUPPORTED if provider_malformed else RULE_UNKNOWN
        return self._result(
            UNKNOWN_EXPIRY_CLASS,
            rule,
            NONE,
            classified_at,
            valid_for,
            diagnostic,
            provider_symbol,
            canonical_symbol,
            raw,
            calendar_class,
        )

    def _calendar_class(self, schedule: ExpirySchedule, expiry: date) -> str | None:
        monthlies = set()
        for year, month in _adjacent_months(expiry.year, expiry.month):
            marked = self._monthly_date(year, month, schedule.monthly_weekday)
            if marked is not None:
                monthlies.add(marked)
        if expiry in monthlies:
            return "MONTHLY"
        if schedule.weekly_weekday is None:
            return None
        weeklies = self._weekly_dates(expiry, schedule.weekly_weekday) - monthlies
        if expiry in weeklies:
            return "WEEKLY"
        return None

    def _monthly_date(self, year: int, month: int, weekday: int | None) -> date | None:
        if weekday is None:
            return None
        raw = last_weekday_of_month(year, month, weekday)
        return previous_session_day(raw, self.holidays)

    def _weekly_dates(self, around: date, weekday: int) -> set[date]:
        start = around.replace(day=1) - timedelta(days=7)
        if around.month == 12:
            end = date(around.year + 1, 1, 1) + timedelta(days=7)
        else:
            end = date(around.year, around.month + 1, 1) + timedelta(days=7)
        found: set[date] = set()
        cursor = start
        while cursor < end:
            if cursor.weekday() == weekday:
                adjusted = previous_session_day(cursor, self.holidays)
                if adjusted is not None:
                    found.add(adjusted)
            cursor += timedelta(days=1)
        return found

    def _result(
        self,
        expiry_class: str,
        rule_id: str,
        evidence_source: str,
        classified_at: datetime,
        valid_for: date,
        diagnostic: str | None,
        provider_symbol: str,
        canonical_symbol: str,
        provider_class: str | None,
        calendar_class: str | None,
    ) -> ExpiryClassification:
        fingerprint = _fingerprint(
            {
                "provider_symbol": provider_symbol,
                "canonical_symbol": canonical_symbol,
                "expiry_class": expiry_class,
                "provider_class": provider_class,
                "calendar_class": calendar_class,
                "rule_id": rule_id,
                "policy_version": self.policy_version,
                "calendar_version": self.calendar_version,
                "valid_for_date": valid_for.isoformat(),
            }
        )
        return ExpiryClassification(
            expiry_class=expiry_class,
            rule_id=rule_id,
            policy_version=self.policy_version,
            evidence_source=evidence_source,
            evidence_fingerprint=fingerprint,
            classified_at=classified_at,
            valid_for_date=valid_for,
            diagnostic=diagnostic,
            calendar_version=self.calendar_version,
            provider_symbol=provider_symbol,
            canonical_symbol=canonical_symbol,
        )


def classification_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    weekly = monthly = unknown = conflicts = excluded = 0
    for row in rows:
        if row.get("option_type") not in {"CE", "PE"}:
            continue
        klass = str(row.get("expiry_class") or UNKNOWN_EXPIRY_CLASS).upper()
        diagnostic = None
        evidence = row.get("classification")
        if isinstance(evidence, Mapping):
            diagnostic = evidence.get("diagnostic")
            klass = str(evidence.get("expiry_class") or klass).upper()
        if diagnostic == CLASSIFICATION_CONFLICT:
            conflicts += 1
        if klass == "WEEKLY":
            weekly += 1
        elif klass == "MONTHLY":
            monthly += 1
        else:
            unknown += 1
            excluded += 1
    return {
        "classified_weekly": weekly,
        "classified_monthly": monthly,
        "unknown": unknown,
        "conflicts": conflicts,
        "excluded": excluded,
    }


def _adjacent_months(year: int, month: int) -> tuple[tuple[int, int], ...]:
    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)
    return ((prev_y, prev_m), (year, month), (next_y, next_m))
