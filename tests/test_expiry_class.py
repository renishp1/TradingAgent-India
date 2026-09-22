from __future__ import annotations

import ast
import pathlib
import unittest
from dataclasses import replace
from datetime import date

from grow.clock import FrozenClock
from grow.errors import GrowConfigError
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.history.resolver import DISALLOWED_CLASS, NO_ELIGIBLE_EXPIRY, resolve_nearest_expiry
from grow.history.universe import (
    IndexUniverseRegistry,
    MONTHLY_ONLY,
    WEEKLY_PREFERRED,
    WEEKLY_THEN_MONTHLY,
    default_index_policies,
    default_index_registry,
)
from grow.live_data.catalog import UNKNOWN_EXPIRY_CLASS
from grow.live_data.expiry_class import (
    CALENDAR,
    CALENDAR_UNSUPPORTED_YEAR,
    CALENDAR_VERSION,
    CLASSIFICATION_CONFLICT,
    CLASSIFIER_NOT_READY,
    ExpiryClassifier,
    NONE,
    POLICY_VERSION,
    PROVIDER,
    classification_counts,
    default_expiry_schedules,
)
from grow.live_data.loop import LivePaperLoop
from grow.live_data.mock import bullish_event
from grow.live_data.models import CycleStatus
from grow.live_data.truedata import TrueDataAdapter
from grow.market.fo_calendar import (
    FO_CALENDAR_VERSION,
    FO_CALENDAR_YEARS,
    FO_EXCLUDED_DATES_2026,
    FO_HOLIDAYS_2026,
    FO_WEEKEND_OBSERVANCES_2026,
)
from grow.market.session import CASH_HOLIDAYS_2026
from grow.paper.positions import PositionState
from tests.helpers import TEST_RISK_SECRET
from tests.test_live_data import AS_OF, _live_config
from tests.test_truedata import (
    _auth_ok,
    _fin_policy,
    _nifty_catalog,
    _real_adapter,
    _settings,
    _symbolsadded,
    _td_symbol,
    _trade,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
NIFTY_WEEKLY = date(2026, 9, 22)
NIFTY_MONTHLY = date(2026, 9, 29)
OLD_THURSDAY_MONTHLY = date(2026, 9, 24)
UNSCHEDULED = date(2026, 9, 21)
MAHAVIR = date(2026, 3, 31)
MAHAVIR_PREV = date(2026, 3, 30)
HOLI = date(2026, 3, 3)
HOLI_PREV = date(2026, 3, 2)
GANESH = date(2026, 9, 14)
NIFTY_2027_WEEKLY = date(2027, 9, 21)
NIFTY_2027_MONTHLY = date(2027, 9, 28)
NIFTY_2027_SHIFTED = date(2027, 9, 27)


def _classify(**kwargs):
    classifier = kwargs.pop("classifier", None) or ExpiryClassifier(clock=FrozenClock(AS_OF))
    defaults = dict(
        provider_symbol="NIFTY26092225000CE",
        canonical_symbol="NIFTY",
        expiry=NIFTY_WEEKLY,
        option_type="CE",
        as_of=AS_OF,
    )
    defaults.update(kwargs)
    return classifier.classify(**defaults)


def _class_of(symbol: str, expiry: date, **kwargs):
    return _classify(
        provider_symbol=f"{symbol}{expiry.strftime('%y%m%d')}25000CE",
        canonical_symbol=symbol,
        expiry=expiry,
        **kwargs,
    )


class ClassifierUnitTests(unittest.TestCase):
    def test_explicit_weekly_and_monthly(self) -> None:
        weekly = _classify(provider_class="WEEKLY")
        self.assertEqual(weekly.expiry_class, "WEEKLY")
        self.assertEqual(weekly.evidence_source, PROVIDER)
        self.assertEqual(weekly.policy_version, POLICY_VERSION)
        self.assertEqual(weekly.calendar_version, FO_CALENDAR_VERSION)
        self.assertIsNotNone(weekly.evidence_fingerprint)
        monthly = _classify(
            provider_symbol="NIFTY26092925000CE",
            expiry=NIFTY_MONTHLY,
            provider_class="MONTHLY",
        )
        self.assertEqual(monthly.expiry_class, "MONTHLY")
        self.assertEqual(monthly.evidence_source, PROVIDER)

    def test_missing_and_malformed_use_calendar_or_unknown(self) -> None:
        missing_weekly = _classify(provider_class=None)
        self.assertEqual(missing_weekly.expiry_class, "WEEKLY")
        self.assertEqual(missing_weekly.evidence_source, CALENDAR)
        missing_unscheduled = _classify(
            provider_symbol="NIFTY26092125000CE",
            expiry=UNSCHEDULED,
            provider_class=None,
        )
        self.assertEqual(missing_unscheduled.expiry_class, UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(missing_unscheduled.evidence_source, NONE)
        self.assertNotEqual(missing_unscheduled.expiry_class, "WEEKLY")
        malformed = _classify(provider_class="QUARTERLY")
        self.assertEqual(malformed.expiry_class, "WEEKLY")
        self.assertEqual(malformed.evidence_source, CALENDAR)
        malformed_unknown = _classify(
            provider_symbol="NIFTY26092125000CE",
            expiry=UNSCHEDULED,
            provider_class="QUARTERLY",
        )
        self.assertEqual(malformed_unknown.expiry_class, UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(malformed_unknown.diagnostic, "PROVIDER_CLASS_UNSUPPORTED")

    def test_calendar_weekly_monthly_and_boundaries(self) -> None:
        self.assertEqual(_classify(provider_class=None).expiry_class, "WEEKLY")
        monthly = _classify(
            provider_symbol="NIFTY26092925000CE",
            expiry=NIFTY_MONTHLY,
            provider_class=None,
        )
        self.assertEqual(monthly.expiry_class, "MONTHLY")
        self.assertEqual(monthly.evidence_source, CALENDAR)
        old_thursday = _classify(
            provider_symbol="NIFTY26092425000CE",
            expiry=OLD_THURSDAY_MONTHLY,
            provider_class=None,
        )
        self.assertEqual(old_thursday.expiry_class, UNKNOWN_EXPIRY_CLASS)
        wednesday = _classify(
            provider_symbol="NIFTY26092325000CE",
            expiry=date(2026, 9, 23),
            provider_class=None,
        )
        self.assertEqual(wednesday.expiry_class, UNKNOWN_EXPIRY_CLASS)

    def test_holiday_adjusts_weekly_to_previous_session(self) -> None:
        holidays = {NIFTY_WEEKLY}
        classifier = ExpiryClassifier(
            clock=FrozenClock(AS_OF),
            holidays=holidays,
            calendar_version="nse.fo.test.holiday.v1",
        )
        shifted = _classify(
            classifier=classifier,
            provider_symbol="NIFTY26092125000CE",
            expiry=UNSCHEDULED,
            provider_class=None,
        )
        self.assertEqual(shifted.expiry_class, "WEEKLY")
        raw_holiday = _classify(
            classifier=classifier,
            expiry=NIFTY_WEEKLY,
            provider_class=None,
        )
        self.assertEqual(raw_holiday.expiry_class, UNKNOWN_EXPIRY_CLASS)

    def test_provider_calendar_conflict_is_unknown(self) -> None:
        conflict = _classify(provider_class="MONTHLY")
        self.assertEqual(conflict.expiry_class, UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(conflict.diagnostic, CLASSIFICATION_CONFLICT)
        self.assertNotEqual(conflict.expiry_class, "WEEKLY")
        self.assertNotEqual(conflict.expiry_class, "MONTHLY")
        other = _classify(
            provider_symbol="NIFTY26092925000CE",
            expiry=NIFTY_MONTHLY,
            provider_class="WEEKLY",
        )
        self.assertEqual(other.diagnostic, CLASSIFICATION_CONFLICT)
        self.assertEqual(other.expiry_class, UNKNOWN_EXPIRY_CLASS)

    def test_unknown_underlying_is_not_ready(self) -> None:
        result = _classify(canonical_symbol="SENSEX", provider_class="WEEKLY")
        self.assertEqual(result.expiry_class, UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(result.diagnostic, CLASSIFIER_NOT_READY)

    def test_fingerprint_stable_and_versioned(self) -> None:
        first = _classify(provider_class=None)
        second = _classify(provider_class=None)
        self.assertEqual(first.evidence_fingerprint, second.evidence_fingerprint)
        bumped = ExpiryClassifier(clock=FrozenClock(AS_OF), policy_version="expiry.class.nse.test")
        other = _classify(classifier=bumped, provider_class=None)
        self.assertEqual(other.policy_version, "expiry.class.nse.test")
        self.assertNotEqual(other.evidence_fingerprint, first.evidence_fingerprint)

    def test_cache_does_not_reuse_other_policy_version(self) -> None:
        classifier = ExpiryClassifier(clock=FrozenClock(AS_OF))
        first = classifier.classify(
            provider_symbol="NIFTY26092225000CE",
            canonical_symbol="NIFTY",
            expiry=NIFTY_WEEKLY,
            option_type="CE",
            as_of=AS_OF,
        )
        classifier.policy_version = "expiry.class.nse.test"
        second = classifier.classify(
            provider_symbol="NIFTY26092225000CE",
            canonical_symbol="NIFTY",
            expiry=NIFTY_WEEKLY,
            option_type="CE",
            as_of=AS_OF,
        )
        self.assertEqual(first.policy_version, POLICY_VERSION)
        self.assertEqual(second.policy_version, "expiry.class.nse.test")
        self.assertNotEqual(first.evidence_fingerprint, second.evidence_fingerprint)


class CurrentNseScheduleTests(unittest.TestCase):
    def test_schedules_match_current_nse_weekdays(self) -> None:
        by_name = {row.canonical_symbol: row for row in default_expiry_schedules()}
        self.assertEqual(by_name["NIFTY"].weekly_weekday, 1)
        self.assertEqual(by_name["NIFTY"].monthly_weekday, 1)
        self.assertIsNone(by_name["BANKNIFTY"].weekly_weekday)
        self.assertEqual(by_name["BANKNIFTY"].monthly_weekday, 1)
        self.assertIsNone(by_name["FINNIFTY"].weekly_weekday)
        self.assertEqual(by_name["FINNIFTY"].monthly_weekday, 1)
        self.assertIsNone(by_name["MIDCPNIFTY"].weekly_weekday)
        self.assertEqual(by_name["MIDCPNIFTY"].monthly_weekday, 1)
        self.assertEqual(POLICY_VERSION, "expiry.class.nse.v2")
        self.assertEqual(CALENDAR_VERSION, "nse.fo.2026.v1")

    def test_nifty_tuesday_weekly_and_last_tuesday_monthly(self) -> None:
        self.assertEqual(_class_of("NIFTY", NIFTY_WEEKLY).expiry_class, "WEEKLY")
        self.assertEqual(_class_of("NIFTY", NIFTY_MONTHLY).expiry_class, "MONTHLY")
        self.assertEqual(_class_of("NIFTY", date(2026, 9, 1)).expiry_class, "WEEKLY")
        self.assertEqual(_class_of("NIFTY", date(2026, 9, 8)).expiry_class, "WEEKLY")
        self.assertEqual(_class_of("NIFTY", date(2026, 9, 15)).expiry_class, "WEEKLY")

    def test_banknifty_has_no_weekly_classification(self) -> None:
        self.assertEqual(_class_of("BANKNIFTY", NIFTY_WEEKLY).expiry_class, UNKNOWN_EXPIRY_CLASS)
        self.assertNotEqual(_class_of("BANKNIFTY", NIFTY_WEEKLY).expiry_class, "WEEKLY")
        self.assertEqual(_class_of("BANKNIFTY", NIFTY_MONTHLY).expiry_class, "MONTHLY")
        self.assertEqual(_class_of("BANKNIFTY", date(2026, 9, 1)).expiry_class, UNKNOWN_EXPIRY_CLASS)

    def test_finnifty_and_midcpnifty_have_no_weekly_classification(self) -> None:
        self.assertEqual(_class_of("FINNIFTY", NIFTY_WEEKLY).expiry_class, UNKNOWN_EXPIRY_CLASS)
        self.assertNotEqual(_class_of("FINNIFTY", NIFTY_WEEKLY).expiry_class, "WEEKLY")
        self.assertEqual(_class_of("FINNIFTY", NIFTY_MONTHLY).expiry_class, "MONTHLY")
        self.assertEqual(_class_of("MIDCPNIFTY", NIFTY_WEEKLY).expiry_class, UNKNOWN_EXPIRY_CLASS)
        self.assertNotEqual(_class_of("MIDCPNIFTY", NIFTY_WEEKLY).expiry_class, "WEEKLY")
        self.assertEqual(_class_of("MIDCPNIFTY", NIFTY_MONTHLY).expiry_class, "MONTHLY")

    def test_weekly_is_not_inferred_from_monthly_weekday(self) -> None:
        schedule = default_expiry_schedules()
        bank = [row for row in schedule if row.canonical_symbol == "BANKNIFTY"][0]
        self.assertEqual(bank.monthly_weekday, 1)
        self.assertIsNone(bank.weekly_weekday)
        self.assertNotEqual(_class_of("BANKNIFTY", NIFTY_WEEKLY).expiry_class, "WEEKLY")


class FoCalendarTests(unittest.TestCase):
    def test_official_2026_holidays_not_cash_seed(self) -> None:
        self.assertEqual(CALENDAR_VERSION, FO_CALENDAR_VERSION)
        self.assertIn(GANESH, FO_HOLIDAYS_2026)
        self.assertIn(MAHAVIR, FO_HOLIDAYS_2026)
        self.assertIn(date(2026, 5, 1), FO_HOLIDAYS_2026)
        self.assertIn(date(2026, 5, 28), FO_HOLIDAYS_2026)
        self.assertIn(date(2026, 6, 26), FO_HOLIDAYS_2026)
        self.assertIn(date(2026, 11, 10), FO_HOLIDAYS_2026)
        self.assertIn(date(2026, 11, 24), FO_HOLIDAYS_2026)
        self.assertNotIn(GANESH, CASH_HOLIDAYS_2026)
        self.assertNotIn(MAHAVIR, CASH_HOLIDAYS_2026)
        for day in FO_WEEKEND_OBSERVANCES_2026 | FO_EXCLUDED_DATES_2026:
            self.assertNotIn(day, FO_HOLIDAYS_2026)
        source = (ROOT / "grow" / "live_data" / "expiry_class.py").read_text(encoding="utf-8")
        self.assertNotIn("CASH_HOLIDAYS", source)
        self.assertNotIn("nse.session.cash", source)
        default = ExpiryClassifier(clock=FrozenClock(AS_OF))
        self.assertEqual(default.holidays, FO_HOLIDAYS_2026)
        self.assertEqual(default.supported_years, FO_CALENDAR_YEARS)
        self.assertNotEqual(default.holidays, CASH_HOLIDAYS_2026)

    def test_ordinary_tuesday_is_weekly_for_nifty(self) -> None:
        result = _class_of("NIFTY", NIFTY_WEEKLY)
        self.assertEqual(result.expiry_class, "WEEKLY")
        self.assertEqual(result.evidence_source, CALENDAR)

    def test_tuesday_holiday_shifts_to_previous_fo_session(self) -> None:
        holi_raw = _class_of("NIFTY", HOLI)
        self.assertEqual(holi_raw.expiry_class, UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(_class_of("NIFTY", HOLI_PREV).expiry_class, "WEEKLY")
        mahavir_raw = _class_of("NIFTY", MAHAVIR)
        self.assertEqual(mahavir_raw.expiry_class, UNKNOWN_EXPIRY_CLASS)
        self.assertNotEqual(mahavir_raw.expiry_class, "WEEKLY")
        self.assertEqual(_class_of("NIFTY", MAHAVIR_PREV).expiry_class, "MONTHLY")
        self.assertEqual(_class_of("BANKNIFTY", MAHAVIR_PREV).expiry_class, "MONTHLY")
        self.assertEqual(_class_of("BANKNIFTY", HOLI_PREV).expiry_class, UNKNOWN_EXPIRY_CLASS)

    def test_unscheduled_and_non_trading_dates_stay_unknown(self) -> None:
        for day in (UNSCHEDULED, OLD_THURSDAY_MONTHLY, GANESH, date(2026, 9, 26), date(2026, 9, 27)):
            result = _class_of("NIFTY", day)
            self.assertEqual(result.expiry_class, UNKNOWN_EXPIRY_CLASS, day.isoformat())
            self.assertNotEqual(result.expiry_class, "WEEKLY", day.isoformat())

    def test_fo_holiday_changes_monthly_vs_cash_seed(self) -> None:
        cash = ExpiryClassifier(
            clock=FrozenClock(AS_OF),
            holidays=CASH_HOLIDAYS_2026,
            calendar_version="nse.session.cash.2026.v1",
        )
        self.assertEqual(_class_of("NIFTY", MAHAVIR, classifier=cash).expiry_class, "MONTHLY")
        self.assertEqual(_class_of("NIFTY", MAHAVIR).expiry_class, UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(_class_of("NIFTY", MAHAVIR_PREV).expiry_class, "MONTHLY")


class UnsupportedYearTests(unittest.TestCase):
    def test_2026_nifty_weekly_and_monthly_unchanged(self) -> None:
        weekly = _class_of("NIFTY", NIFTY_WEEKLY)
        monthly = _class_of("NIFTY", NIFTY_MONTHLY)
        self.assertEqual(weekly.expiry_class, "WEEKLY")
        self.assertEqual(weekly.evidence_source, CALENDAR)
        self.assertEqual(weekly.calendar_version, "nse.fo.2026.v1")
        self.assertEqual(monthly.expiry_class, "MONTHLY")
        self.assertEqual(monthly.evidence_source, CALENDAR)

    def test_2027_nifty_expiry_is_unknown_with_2026_calendar(self) -> None:
        classifier = ExpiryClassifier(clock=FrozenClock(AS_OF))
        self.assertEqual(classifier.calendar_version, "nse.fo.2026.v1")
        self.assertEqual(classifier.supported_years, frozenset({2026}))
        for expiry, provider_class in (
            (NIFTY_2027_WEEKLY, None),
            (NIFTY_2027_MONTHLY, None),
            (NIFTY_2027_WEEKLY, "WEEKLY"),
            (NIFTY_2027_MONTHLY, "MONTHLY"),
        ):
            result = _class_of("NIFTY", expiry, classifier=classifier, provider_class=provider_class)
            self.assertEqual(result.expiry_class, UNKNOWN_EXPIRY_CLASS, expiry.isoformat())
            self.assertEqual(result.diagnostic, CALENDAR_UNSUPPORTED_YEAR, expiry.isoformat())
            self.assertEqual(result.evidence_source, NONE, expiry.isoformat())
            self.assertNotEqual(result.expiry_class, "WEEKLY")
            self.assertNotEqual(result.expiry_class, "MONTHLY")

    def test_2027_holiday_adjustment_is_never_attempted_with_2026_holidays(self) -> None:
        from grow.live_data import expiry_class as expiry_mod

        planted = FO_HOLIDAYS_2026 | {NIFTY_2027_MONTHLY}
        classifier = ExpiryClassifier(
            clock=FrozenClock(AS_OF),
            holidays=planted,
            calendar_version=FO_CALENDAR_VERSION,
        )
        original = expiry_mod.previous_session_day

        def guarded(day: date, holidays) -> date | None:
            self.assertNotEqual(day.year, 2027, f"holiday-adjusted {day.isoformat()}")
            for item in holidays:
                self.assertNotEqual(item.year, 2027, f"used {item.isoformat()} from another year")
            return original(day, holidays)

        expiry_mod.previous_session_day = guarded
        try:
            monthly_like = _class_of("NIFTY", NIFTY_2027_MONTHLY, classifier=classifier)
            shifted_like = _class_of("NIFTY", NIFTY_2027_SHIFTED, classifier=classifier)
            weekly_like = _class_of("NIFTY", NIFTY_2027_WEEKLY, classifier=classifier)
            self.assertIsNone(classifier._calendar_class(classifier.schedules[0], NIFTY_2027_MONTHLY))
            self.assertIsNone(classifier._calendar_class(classifier.schedules[0], NIFTY_2027_WEEKLY))
        finally:
            expiry_mod.previous_session_day = original
        for result in (monthly_like, shifted_like, weekly_like):
            self.assertEqual(result.expiry_class, UNKNOWN_EXPIRY_CLASS)
            self.assertEqual(result.diagnostic, CALENDAR_UNSUPPORTED_YEAR)
            self.assertEqual(result.evidence_source, NONE)
            self.assertNotEqual(result.expiry_class, "WEEKLY")
            self.assertNotEqual(result.expiry_class, "MONTHLY")

    def test_2027_unknown_is_excluded_from_truedata_subscription(self) -> None:
        adapter = TrueDataAdapter(
            events=(),
            catalog=[
                {"provider_symbol": "NIFTY 50", "lot_size": None},
                {"provider_symbol": "NIFTY26092225000CE", "lot_size": 75, "expiry": NIFTY_WEEKLY, "option_type": "CE"},
                {"provider_symbol": "NIFTY27092125000CE", "lot_size": 75, "expiry": NIFTY_2027_WEEKLY, "option_type": "CE"},
                {"provider_symbol": "NIFTY27092825000CE", "lot_size": 75, "expiry": NIFTY_2027_MONTHLY, "option_type": "CE"},
            ],
            settings=_settings(),
            clock=FrozenClock(AS_OF),
        )
        adapter._spots["NIFTY"] = 25000.0
        adapter.connect()
        by_symbol = {row["provider_symbol"]: row for row in adapter.instrument_catalog()}
        self.assertEqual(by_symbol["NIFTY26092225000CE"]["expiry_class"], "WEEKLY")
        self.assertEqual(by_symbol["NIFTY27092125000CE"]["expiry_class"], UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(by_symbol["NIFTY27092125000CE"]["classification"]["diagnostic"], CALENDAR_UNSUPPORTED_YEAR)
        self.assertEqual(by_symbol["NIFTY27092825000CE"]["expiry_class"], UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(by_symbol["NIFTY27092825000CE"]["classification"]["diagnostic"], CALENDAR_UNSUPPORTED_YEAR)
        self.assertTrue(any("NIFTY26092225000CE" in symbol for symbol in adapter._desired))
        self.assertFalse(any("NIFTY27092125000CE" in symbol for symbol in adapter._desired))
        self.assertFalse(any("NIFTY27092825000CE" in symbol for symbol in adapter._desired))
        counts = classification_counts(adapter.instrument_catalog())
        self.assertGreaterEqual(counts["unknown"], 2)
        self.assertGreaterEqual(counts["excluded"], 2)


class PolicyIntegrationTests(unittest.TestCase):
    def test_default_overlay_banknifty_is_monthly_only(self) -> None:
        reg = default_index_registry()
        self.assertEqual(reg.policy("NIFTY").expiry_policy_profile, WEEKLY_PREFERRED)
        self.assertEqual(reg.policy("BANKNIFTY").expiry_policy_profile, MONTHLY_ONLY)
        self.assertEqual(reg.policy("MIDCPNIFTY").expiry_policy_profile, MONTHLY_ONLY)
        self.assertIsNone(reg.policy("FINNIFTY"))

    def test_weekly_then_monthly_and_monthly_only(self) -> None:
        weekly = NIFTY_WEEKLY
        monthly = NIFTY_MONTHLY
        adapter = TrueDataAdapter(
            events=(),
            catalog=[
                {"provider_symbol": "NIFTY 50", "lot_size": None},
                {"provider_symbol": "NIFTY26092225000CE", "lot_size": 75, "expiry": weekly, "option_type": "CE"},
                {"provider_symbol": "NIFTY26092925000CE", "lot_size": 75, "expiry": monthly, "option_type": "CE"},
            ],
            settings=_settings(),
            clock=FrozenClock(AS_OF),
        )
        records = adapter._expiry_records("NIFTY")
        preferred = resolve_nearest_expiry("NIFTY", AS_OF, records, WEEKLY_PREFERRED, allow_same_day=False)
        self.assertEqual(preferred.selected_expiry, weekly.isoformat())
        self.assertEqual(preferred.selected_expiry_class, "WEEKLY")
        then = resolve_nearest_expiry("NIFTY", AS_OF, records, WEEKLY_THEN_MONTHLY, allow_same_day=False)
        self.assertEqual(then.selected_expiry, weekly.isoformat())
        monthly_only = resolve_nearest_expiry("NIFTY", AS_OF, records, MONTHLY_ONLY, allow_same_day=False)
        self.assertEqual(monthly_only.selected_expiry, monthly.isoformat())
        self.assertEqual(monthly_only.selected_expiry_class, "MONTHLY")

    def test_monthly_only_rejects_weekly_and_unknown(self) -> None:
        catalog = [
            {"provider_symbol": "NIFTY 50", "lot_size": None},
            {"provider_symbol": "NIFTY26092225000CE", "lot_size": 75, "expiry": NIFTY_WEEKLY, "option_type": "CE"},
            {"provider_symbol": "NIFTY26092125000CE", "lot_size": 75, "expiry": UNSCHEDULED, "option_type": "CE"},
            {"provider_symbol": "NIFTY26092925000CE", "lot_size": 75, "expiry": NIFTY_MONTHLY, "option_type": "CE"},
        ]
        nifty_monthly = tuple(
            replace(p, expiry_policy_profile=MONTHLY_ONLY) if p.canonical_symbol == "NIFTY" else p
            for p in default_index_policies()
        )
        adapter = TrueDataAdapter(
            events=(),
            catalog=catalog,
            settings=_settings(),
            clock=FrozenClock(AS_OF),
            registry=IndexUniverseRegistry(nifty_monthly),
        )
        adapter._spots["NIFTY"] = 25000.0
        adapter.connect()
        records = adapter._expiry_records("NIFTY")
        by_day = {rec.expiry: rec.expiry_class for rec in records}
        self.assertEqual(by_day[NIFTY_WEEKLY], "WEEKLY")
        self.assertEqual(by_day[UNSCHEDULED], UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(by_day[NIFTY_MONTHLY], "MONTHLY")
        resolved = resolve_nearest_expiry("NIFTY", AS_OF, records, MONTHLY_ONLY, allow_same_day=False)
        self.assertEqual(resolved.selected_expiry, NIFTY_MONTHLY.isoformat())
        self.assertEqual(resolved.selected_expiry_class, "MONTHLY")
        self.assertTrue(any(DISALLOWED_CLASS in reason for reason in resolved.exclusion_reasons))
        self.assertTrue(any("NIFTY26092925000CE" in symbol for symbol in adapter._desired))
        self.assertFalse(any("NIFTY26092225000CE" in symbol for symbol in adapter._desired))
        self.assertFalse(any("NIFTY26092125000CE" in symbol for symbol in adapter._desired))
        empty = resolve_nearest_expiry(
            "NIFTY",
            AS_OF,
            tuple(rec for rec in records if rec.expiry != NIFTY_MONTHLY),
            MONTHLY_ONLY,
            allow_same_day=False,
        )
        self.assertIsNone(empty.selected_expiry)
        self.assertTrue(
            any(DISALLOWED_CLASS in reason or NO_ELIGIBLE_EXPIRY in reason for reason in empty.exclusion_reasons)
        )

    def test_unknown_excluded_from_subscription(self) -> None:
        adapter = TrueDataAdapter(
            events=(),
            catalog=[
                {"provider_symbol": "NIFTY 50", "lot_size": None},
                {"provider_symbol": "NIFTY26092125000CE", "lot_size": 75, "expiry": UNSCHEDULED, "option_type": "CE"},
                {"provider_symbol": "NIFTY26092225000CE", "lot_size": 75, "expiry": NIFTY_WEEKLY, "option_type": "CE"},
            ],
            settings=_settings(),
            clock=FrozenClock(AS_OF),
        )
        adapter._spots["NIFTY"] = 25000.0
        adapter.connect()
        self.assertTrue(any("NIFTY26092225000CE" in s for s in adapter._desired))
        self.assertFalse(any("NIFTY26092125000CE" in s for s in adapter._desired))
        counts = classification_counts(adapter.instrument_catalog())
        self.assertGreaterEqual(counts["classified_weekly"], 1)
        self.assertGreaterEqual(counts["unknown"], 1)
        self.assertGreaterEqual(counts["excluded"], 1)

    def test_dynamic_multi_underlying_catalog(self) -> None:
        policies = default_index_policies() + (_fin_policy(),)
        overlay = IndexUniverseRegistry(policies)
        self.assertEqual(overlay.policy("FINNIFTY").expiry_policy_profile, MONTHLY_ONLY)
        adapter = TrueDataAdapter(
            events=(),
            catalog=[
                {"provider_symbol": "NIFTY 50", "lot_size": None},
                {"provider_symbol": "NIFTY BANK", "lot_size": None},
                {"provider_symbol": "NIFTY26092225000CE", "lot_size": 75},
                {"provider_symbol": "BANKNIFTY26092252000CE", "lot_size": 15},
                {"provider_symbol": "BANKNIFTY26092952000CE", "lot_size": 15},
                {"provider_symbol": "FINNIFTY26092225000CE", "lot_size": 40},
                {"provider_symbol": "FINNIFTY26092925000CE", "lot_size": 40},
                {"provider_symbol": "MIDCPNIFTY26092213000CE", "lot_size": 75},
                {"provider_symbol": "MIDCPNIFTY26092913000CE", "lot_size": 75},
            ],
            settings=_settings(),
            clock=FrozenClock(AS_OF),
            registry=overlay,
        )
        adapter._spots.update({"NIFTY": 25000.0, "BANKNIFTY": 52000.0, "FINNIFTY": 25000.0, "MIDCPNIFTY": 13000.0})
        adapter.connect()
        discovered = adapter.discover_underlyings()
        self.assertIn("NIFTY", discovered)
        self.assertIn("BANKNIFTY", discovered)
        self.assertIn("FINNIFTY", discovered)
        self.assertIn("MIDCPNIFTY", discovered)
        by_symbol = {row["provider_symbol"]: row["expiry_class"] for row in adapter.instrument_catalog()}
        self.assertEqual(by_symbol["NIFTY26092225000CE"], "WEEKLY")
        self.assertEqual(by_symbol["BANKNIFTY26092252000CE"], UNKNOWN_EXPIRY_CLASS)
        self.assertNotEqual(by_symbol["BANKNIFTY26092252000CE"], "WEEKLY")
        self.assertEqual(by_symbol["BANKNIFTY26092952000CE"], "MONTHLY")
        self.assertEqual(by_symbol["FINNIFTY26092225000CE"], UNKNOWN_EXPIRY_CLASS)
        self.assertNotEqual(by_symbol["FINNIFTY26092225000CE"], "WEEKLY")
        self.assertEqual(by_symbol["FINNIFTY26092925000CE"], "MONTHLY")
        self.assertEqual(by_symbol["MIDCPNIFTY26092213000CE"], UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(by_symbol["MIDCPNIFTY26092913000CE"], "MONTHLY")
        self.assertTrue(any("NIFTY260922" in s for s in adapter._desired))
        self.assertFalse(any("BANKNIFTY260922" in s for s in adapter._desired))
        self.assertTrue(any("BANKNIFTY260929" in s for s in adapter._desired))
        self.assertFalse(any("FINNIFTY260922" in s for s in adapter._desired))
        self.assertTrue(any("FINNIFTY260929" in s for s in adapter._desired))
        self.assertFalse(any("MIDCPNIFTY260922" in s for s in adapter._desired))
        self.assertTrue(any("MIDCPNIFTY260929" in s for s in adapter._desired))
        for symbol in adapter._desired:
            if symbol.endswith(("CE", "PE")):
                self.assertIn(by_symbol.get(symbol), {"WEEKLY", "MONTHLY"})


class ReconnectClassificationTests(unittest.TestCase):
    def test_reconnect_reclassifies_under_new_policy_version(self) -> None:
        catalog = [
            {"provider_symbol": "NIFTY 50", "lot_size": None},
            {"provider_symbol": "NIFTY26092225000CE", "lot_size": 75},
        ]
        classifier = ExpiryClassifier(clock=FrozenClock(AS_OF))
        adapter = TrueDataAdapter(
            events=(),
            catalog=catalog,
            settings=_settings(reconnect_policy="bounded_backoff", max_attempts=3, max_backoff_seconds=8),
            clock=FrozenClock(AS_OF),
            classifier=classifier,
        )
        adapter.connect()
        first = adapter.instrument_catalog()[1]["classification"]["policy_version"]
        self.assertEqual(first, POLICY_VERSION)
        classifier.policy_version = "expiry.class.nse.test"
        classifier.invalidate()
        adapter._reclassify_instruments()
        second = [row for row in adapter.instrument_catalog() if row.get("option_type") == "CE"][0]
        self.assertEqual(second["classification"]["policy_version"], "expiry.class.nse.test")
        self.assertEqual(second["expiry_class"], "WEEKLY")


class EndToEndClassificationTests(unittest.TestCase):
    def test_catalog_without_class_opens_and_closes_paper(self) -> None:
        mixed = TrueDataAdapter(
            events=(),
            catalog=[
                {"provider_symbol": "NIFTY 50", "lot_size": None},
                {"provider_symbol": "NIFTY BANK", "lot_size": None},
                {"provider_symbol": "NIFTY26092225000CE", "lot_size": 75, "expiry": NIFTY_WEEKLY, "option_type": "CE"},
                {"provider_symbol": "NIFTY26092125000CE", "lot_size": 75, "expiry": UNSCHEDULED, "option_type": "CE"},
                {"provider_symbol": "NIFTY26092425000CE", "lot_size": 75, "expiry": OLD_THURSDAY_MONTHLY, "option_type": "CE"},
                {"provider_symbol": "BANKNIFTY26092252000CE", "lot_size": 15, "expiry": NIFTY_WEEKLY, "option_type": "CE"},
                {"provider_symbol": "BANKNIFTY26092952000CE", "lot_size": 15, "expiry": NIFTY_MONTHLY, "option_type": "CE"},
            ],
            settings=_settings(),
            clock=FrozenClock(AS_OF),
        )
        mixed._spots.update({"NIFTY": 25000.0, "BANKNIFTY": 52000.0})
        mixed.connect()
        by_symbol = {row["provider_symbol"]: row for row in mixed.instrument_catalog()}
        self.assertEqual(by_symbol["NIFTY26092225000CE"]["expiry_class"], "WEEKLY")
        self.assertEqual(by_symbol["NIFTY26092225000CE"]["classification"]["evidence_source"], CALENDAR)
        self.assertEqual(by_symbol["NIFTY26092225000CE"]["classification"]["calendar_version"], FO_CALENDAR_VERSION)
        self.assertEqual(by_symbol["BANKNIFTY26092252000CE"]["expiry_class"], UNKNOWN_EXPIRY_CLASS)
        self.assertNotEqual(by_symbol["BANKNIFTY26092252000CE"]["expiry_class"], "WEEKLY")
        self.assertEqual(by_symbol["BANKNIFTY26092952000CE"]["expiry_class"], "MONTHLY")
        self.assertEqual(by_symbol["NIFTY26092125000CE"]["expiry_class"], UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(by_symbol["NIFTY26092425000CE"]["expiry_class"], UNKNOWN_EXPIRY_CLASS)
        self.assertTrue(any("NIFTY260922" in s for s in mixed._desired))
        self.assertFalse(any("BANKNIFTY260922" in s for s in mixed._desired))
        self.assertTrue(any("BANKNIFTY260929" in s for s in mixed._desired))
        self.assertFalse(any("NIFTY260921" in s for s in mixed._desired))
        self.assertFalse(any("NIFTY260924" in s for s in mixed._desired))
        for symbol in mixed._desired:
            if symbol.endswith(("CE", "PE")):
                self.assertIn(by_symbol[symbol]["expiry_class"], {"WEEKLY", "MONTHLY"})

        catalog = _nifty_catalog()
        for row in catalog["instruments"]:
            row.pop("expiry_class", None)
            row.pop("class", None)
        ticks = [_trade(catalog["mapping"][0][1], float(catalog["spots"]["NIFTY"]), seq=1)]
        seq = 2
        event = bullish_event(underlyings=("NIFTY",))
        by_master = {
            _td_symbol(row["underlying"], row["expiry"], row["strike"], row["option_type"]): row
            for row in event["contract_master"]
            if row["underlying"] == "NIFTY"
        }
        quotes_by = {(q["underlying"], q["expiry"], float(q["strike"]), q["option_type"]): q for q in catalog["quotes"]}
        for name, ident in catalog["mapping"][1:]:
            master = by_master.get(name)
            if master is None:
                continue
            quote = quotes_by.get(("NIFTY", master["expiry"], float(master["strike"]), master["option_type"]))
            if quote is None:
                continue
            ticks.append(
                _trade(
                    ident,
                    float(quote["ltp"]),
                    bid=quote["bid"],
                    ask=quote["ask"],
                    seq=seq,
                    oi=quote["oi"],
                    volume=quote["volume"],
                )
            )
            seq += 1
        incoming = [_auth_ok(), _symbolsadded(catalog["mapping"])] + ticks
        adapter, socket = _real_adapter(incoming, catalog)
        cfg = _live_config(provider="truedata", snapshot_interval_seconds=0, session_timeout_seconds=86400)
        loop = LivePaperLoop(cfg, adapter, clock=FrozenClock(AS_OF), risk_secret=TEST_RISK_SECRET)
        loop.start()
        classes = {row["expiry_class"] for row in adapter.instrument_catalog() if row.get("option_type") in {"CE", "PE"}}
        self.assertIn("WEEKLY", classes)
        self.assertTrue(any("NIFTY260922" in s for s in adapter._desired))
        self.assertFalse(any("NIFTY260924" in s for s in adapter._desired))
        evidence = [row.get("classification") for row in adapter.instrument_catalog() if row.get("option_type") == "CE"]
        self.assertTrue(any(item and item.get("evidence_source") == CALENDAR for item in evidence))
        opened = None
        for _ in range(len(ticks) + 4):
            reports = loop.run_once("NIFTY")
            for report in reports:
                if report.status == CycleStatus.PAPER_FILL:
                    opened = report
                    break
            if opened:
                break
        self.assertIsNotNone(opened)
        self.assertEqual(opened.status, CycleStatus.PAPER_FILL, opened.reason if opened else "no fill")
        self.assertTrue(adapter._mapping_ready)
        self.assertEqual(loop.positions.open_positions()[0].lot_size, 75)
        pos = loop.positions.open_positions()[0]
        stop_ticks = []
        stop_seq = 900
        for _name, ident in catalog["mapping"][1:]:
            if pos.contract_id.split("-")[-1] == "CE" and "CE" not in _name:
                continue
            if pos.contract_id.split("-")[-1] == "PE" and "PE" not in _name:
                continue
            stop_ticks.append(_trade(ident, 10.0, bid=10.0, ask=11.0, seq=stop_seq, oi=5000, volume=200))
            stop_seq += 1
        socket.incoming[:] = stop_ticks
        closed = None
        for _ in range(len(stop_ticks) + 8):
            reports = loop.run_once("NIFTY")
            for report in reports:
                if report.status == CycleStatus.PAPER_CLOSE:
                    closed = report
                    break
            if closed:
                break
        self.assertIsNotNone(closed)
        self.assertEqual(loop.positions.all()[0].state, PositionState.CLOSED)

    def test_no_fixture_fallback_or_broker(self) -> None:
        self.assertFalse(LIVE_TRADING_COMPILED)
        loop = TrueDataAdapter(
            events=[{"provider": "grow.data.fixture.v1", "is_fixture": True, "sequence": 1}],
            settings=_settings(),
            clock=FrozenClock(AS_OF),
        )
        loop.connect()
        with self.assertRaises(GrowConfigError) as ctx:
            loop.poll()
        self.assertIn("FIXTURE_FALLBACK_FORBIDDEN", str(ctx.exception))
        source = (ROOT / "grow" / "live_data" / "expiry_class.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertTrue({"place_order", "LiveBroker", "kiteconnect"}.isdisjoint(names))
        self.assertNotIn("kiteconnect", source.lower())
