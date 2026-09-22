from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import date

from grow.clock import FrozenClock
from grow.errors import GrowConfigError
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.history.resolver import resolve_nearest_expiry
from grow.history.universe import (
    IndexUniverseRegistry,
    MONTHLY_ONLY,
    WEEKLY_PREFERRED,
    WEEKLY_THEN_MONTHLY,
    default_index_policies,
)
from grow.live_data.catalog import UNKNOWN_EXPIRY_CLASS
from grow.live_data.expiry_class import (
    CALENDAR,
    CLASSIFICATION_CONFLICT,
    CLASSIFIER_NOT_READY,
    ExpiryClassifier,
    NONE,
    POLICY_VERSION,
    PROVIDER,
    classification_counts,
)
from grow.live_data.loop import LivePaperLoop
from grow.live_data.mock import bullish_event
from grow.live_data.models import CycleStatus
from grow.live_data.truedata import TrueDataAdapter
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


def _classify(**kwargs):
    classifier = kwargs.pop("classifier", None) or ExpiryClassifier(clock=FrozenClock(AS_OF))
    defaults = dict(
        provider_symbol="NIFTY26092225000CE",
        canonical_symbol="NIFTY",
        expiry=date(2026, 9, 22),
        option_type="CE",
        as_of=AS_OF,
    )
    defaults.update(kwargs)
    return classifier.classify(**defaults)


class ClassifierUnitTests(unittest.TestCase):
    def test_explicit_weekly_and_monthly(self) -> None:
        weekly = _classify(provider_class="WEEKLY")
        self.assertEqual(weekly.expiry_class, "WEEKLY")
        self.assertEqual(weekly.evidence_source, PROVIDER)
        self.assertEqual(weekly.policy_version, POLICY_VERSION)
        self.assertIsNotNone(weekly.evidence_fingerprint)
        monthly = _classify(
            provider_symbol="NIFTY26092425000CE",
            expiry=date(2026, 9, 24),
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
            expiry=date(2026, 9, 21),
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
            expiry=date(2026, 9, 21),
            provider_class="QUARTERLY",
        )
        self.assertEqual(malformed_unknown.expiry_class, UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(malformed_unknown.diagnostic, "PROVIDER_CLASS_UNSUPPORTED")

    def test_calendar_weekly_monthly_and_boundaries(self) -> None:
        self.assertEqual(_classify(provider_class=None).expiry_class, "WEEKLY")
        monthly = _classify(
            provider_symbol="NIFTY26092425000CE",
            expiry=date(2026, 9, 24),
            provider_class=None,
        )
        self.assertEqual(monthly.expiry_class, "MONTHLY")
        self.assertEqual(monthly.evidence_source, CALENDAR)
        end_month_weekly = _classify(
            provider_symbol="NIFTY26092925000CE",
            expiry=date(2026, 9, 29),
            provider_class=None,
        )
        self.assertEqual(end_month_weekly.expiry_class, "WEEKLY")
        wednesday = _classify(
            provider_symbol="NIFTY26092325000CE",
            expiry=date(2026, 9, 23),
            provider_class=None,
        )
        self.assertEqual(wednesday.expiry_class, UNKNOWN_EXPIRY_CLASS)

    def test_holiday_adjusts_weekly_to_previous_session(self) -> None:
        holidays = {date(2026, 9, 22)}
        classifier = ExpiryClassifier(
            clock=FrozenClock(AS_OF),
            holidays=holidays,
            calendar_version="nse.session.test.holiday.v1",
        )
        shifted = _classify(
            classifier=classifier,
            provider_symbol="NIFTY26092125000CE",
            expiry=date(2026, 9, 21),
            provider_class=None,
        )
        self.assertEqual(shifted.expiry_class, "WEEKLY")
        raw_holiday = _classify(
            classifier=classifier,
            expiry=date(2026, 9, 22),
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
            provider_symbol="NIFTY26092425000CE",
            expiry=date(2026, 9, 24),
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
        bumped = ExpiryClassifier(clock=FrozenClock(AS_OF), policy_version="expiry.class.nse.v2")
        other = _classify(classifier=bumped, provider_class=None)
        self.assertEqual(other.policy_version, "expiry.class.nse.v2")
        self.assertNotEqual(other.evidence_fingerprint, first.evidence_fingerprint)

    def test_cache_does_not_reuse_other_policy_version(self) -> None:
        classifier = ExpiryClassifier(clock=FrozenClock(AS_OF))
        first = classifier.classify(
            provider_symbol="NIFTY26092225000CE",
            canonical_symbol="NIFTY",
            expiry=date(2026, 9, 22),
            option_type="CE",
            as_of=AS_OF,
        )
        classifier.policy_version = "expiry.class.nse.v2"
        second = classifier.classify(
            provider_symbol="NIFTY26092225000CE",
            canonical_symbol="NIFTY",
            expiry=date(2026, 9, 22),
            option_type="CE",
            as_of=AS_OF,
        )
        self.assertEqual(first.policy_version, POLICY_VERSION)
        self.assertEqual(second.policy_version, "expiry.class.nse.v2")
        self.assertNotEqual(first.evidence_fingerprint, second.evidence_fingerprint)


class PolicyIntegrationTests(unittest.TestCase):
    def test_weekly_then_monthly_and_monthly_only(self) -> None:
        weekly = date(2026, 9, 22)
        monthly = date(2026, 9, 24)
        adapter = TrueDataAdapter(
            events=(),
            catalog=[
                {"provider_symbol": "NIFTY 50", "lot_size": None},
                {"provider_symbol": "NIFTY26092225000CE", "lot_size": 75, "expiry": weekly, "option_type": "CE"},
                {"provider_symbol": "NIFTY26092425000CE", "lot_size": 75, "expiry": monthly, "option_type": "CE"},
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

    def test_unknown_excluded_from_subscription(self) -> None:
        adapter = TrueDataAdapter(
            events=(),
            catalog=[
                {"provider_symbol": "NIFTY 50", "lot_size": None},
                {"provider_symbol": "NIFTY26092125000CE", "lot_size": 75, "expiry": date(2026, 9, 21), "option_type": "CE"},
                {"provider_symbol": "NIFTY26092225000CE", "lot_size": 75, "expiry": date(2026, 9, 22), "option_type": "CE"},
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
        adapter = TrueDataAdapter(
            events=(),
            catalog=[
                {"provider_symbol": "NIFTY 50", "lot_size": None},
                {"provider_symbol": "NIFTY BANK", "lot_size": None},
                {"provider_symbol": "NIFTY26092225000CE", "lot_size": 75},
                {"provider_symbol": "BANKNIFTY26092252000CE", "lot_size": 15},
                {"provider_symbol": "FINNIFTY26092225000CE", "lot_size": 40},
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
        self.assertEqual(by_symbol["BANKNIFTY26092252000CE"], "WEEKLY")
        self.assertEqual(by_symbol["FINNIFTY26092225000CE"], "WEEKLY")
        self.assertEqual(by_symbol["MIDCPNIFTY26092213000CE"], UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(by_symbol["MIDCPNIFTY26092913000CE"], "MONTHLY")
        self.assertTrue(any("NIFTY260922" in s for s in adapter._desired))
        self.assertFalse(any("MIDCPNIFTY260922" in s for s in adapter._desired))
        self.assertTrue(any("MIDCPNIFTY260929" in s for s in adapter._desired))


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
        classifier.policy_version = "expiry.class.nse.v2"
        classifier.invalidate()
        adapter._reclassify_instruments()
        second = [row for row in adapter.instrument_catalog() if row.get("option_type") == "CE"][0]
        self.assertEqual(second["classification"]["policy_version"], "expiry.class.nse.v2")
        self.assertEqual(second["expiry_class"], "WEEKLY")


class EndToEndClassificationTests(unittest.TestCase):
    def test_catalog_without_class_opens_paper(self) -> None:
        catalog = _nifty_catalog()
        for row in catalog["instruments"]:
            row.pop("expiry_class", None)
        ticks = [_trade(catalog["mapping"][0][1], float(catalog["spots"]["NIFTY"]), seq=1)]
        seq = 2
        event = bullish_event(underlyings=("NIFTY",))
        by_symbol = {
            _td_symbol(row["underlying"], row["expiry"], row["strike"], row["option_type"]): row
            for row in event["contract_master"]
            if row["underlying"] == "NIFTY"
        }
        quotes_by = {(q["underlying"], q["expiry"], float(q["strike"]), q["option_type"]): q for q in catalog["quotes"]}
        for name, ident in catalog["mapping"][1:]:
            master = by_symbol.get(name)
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
        adapter, _sock = _real_adapter(incoming, catalog)
        cfg = _live_config(provider="truedata", snapshot_interval_seconds=0, session_timeout_seconds=86400)
        loop = LivePaperLoop(cfg, adapter, clock=FrozenClock(AS_OF), risk_secret=TEST_RISK_SECRET)
        loop.start()
        classes = {row["expiry_class"] for row in adapter.instrument_catalog() if row.get("option_type") in {"CE", "PE"}}
        self.assertIn("WEEKLY", classes)
        self.assertNotIn(UNKNOWN_EXPIRY_CLASS, {c for c in classes if c == "WEEKLY"})
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
        self.assertEqual(loop.positions.open_positions()[0].lot_size, 75)
        evidence = [row.get("classification") for row in adapter.instrument_catalog() if row.get("option_type") == "CE"]
        self.assertTrue(any(item and item.get("evidence_source") in {CALENDAR, PROVIDER} for item in evidence))

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
