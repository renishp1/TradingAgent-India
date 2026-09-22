from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta
from dataclasses import replace

from grow.backtest.runner import build_manifest
from grow.clock import IST
from grow.config import load_config
from grow.director.catalog import ApprovedDataSource, consume_qualification, default_catalog, require_approved_for_2e
from grow.director.validate import ResearchPlanValidator
from grow.errors import GrowConfigError
from grow.history.bridge import HistoricalMarketSource, HistoricalOptionSource
from grow.history.eval import ProviderEvaluationRunner, derive_quality_warnings
from grow.history.eval_sample import (
    EVAL_ID,
    MONTHLY,
    WEEKLY_LATE,
    WEEKLY_NEXT,
    WEEKLY_SAME,
    build_eval_store,
)
from grow.history.expiry import select_nearest_weekly_expiry, universe_at
from grow.history.models import (
    APPROVED_FOR_2E,
    CANDIDATE,
    FRAMEWORK_TEST_ONLY,
    OPTION_SNAPSHOT_GAPS,
    QUALIFIED_WITH_WARNINGS,
    HistoricalOptionContract,
    HistoricalOptionQuote,
)
from grow.history.provider import export_vendor_payload, ingest_vendor_payload
from grow.history.sample import SAMPLE_ID
from grow.history.store import CanonicalStore
from grow.options.select import choose_expiry
from grow.types import SessionState
from tests.test_director import _valid_plan
from tests.test_history import _meta, _session


def _ts(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)


class NearestExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = build_eval_store()
        self.cfg = load_config().options

    def test_same_day_weekly_excluded(self) -> None:
        as_of = _ts(WEEKLY_SAME, 11, 0)
        visible = universe_at(self.store, "NIFTY", as_of)
        days = {r.expiry: r.expiry_class for r in visible}
        self.assertIn(WEEKLY_SAME, days)
        chosen = select_nearest_weekly_expiry("NIFTY", as_of, visible, allow_same_day=False)
        self.assertEqual(chosen, WEEKLY_NEXT)
        self.assertNotEqual(chosen, WEEKLY_SAME)

    def test_nearest_of_multiple_weeklies(self) -> None:
        as_of = _ts(date(2026, 9, 14), 11, 0)
        visible = universe_at(self.store, "NIFTY", as_of)
        self.assertEqual(select_nearest_weekly_expiry("NIFTY", as_of, visible), WEEKLY_SAME)
        self.assertIn(WEEKLY_NEXT, {r.expiry for r in visible})

    def test_weekly_preferred_over_monthly(self) -> None:
        as_of = _ts(date(2026, 9, 14), 11, 0)
        visible = universe_at(self.store, "NIFTY", as_of)
        self.assertIn(MONTHLY, {r.expiry for r in visible})
        self.assertEqual(select_nearest_weekly_expiry("NIFTY", as_of, visible), WEEKLY_SAME)

    def test_late_listed_weekly_excluded_before_first_seen(self) -> None:
        as_of = _ts(WEEKLY_SAME, 11, 0)
        visible = universe_at(self.store, "NIFTY", as_of)
        self.assertNotIn(WEEKLY_LATE, {r.expiry for r in visible})
        later = universe_at(self.store, "NIFTY", _ts(date(2026, 9, 16), 11, 0))
        self.assertIn(WEEKLY_LATE, {r.expiry for r in later})

    def test_expired_weekly_excluded(self) -> None:
        as_of = _ts(date(2026, 9, 16), 11, 0)
        visible = universe_at(self.store, "NIFTY", as_of)
        self.assertNotIn(WEEKLY_SAME, {r.expiry for r in visible})
        self.assertEqual(select_nearest_weekly_expiry("NIFTY", as_of, visible), WEEKLY_NEXT)

    def test_no_future_weekly_is_no_trade(self) -> None:
        as_of = _ts(date(2026, 9, 30), 11, 0)
        visible = universe_at(self.store, "NIFTY", as_of)
        self.assertIsNone(select_nearest_weekly_expiry("NIFTY", as_of, visible))

    def test_boundary_adjacent_timestamps(self) -> None:
        before = _ts(WEEKLY_SAME, 15, 15)
        after = _ts(date(2026, 9, 16), 11, 0)
        self.assertEqual(select_nearest_weekly_expiry("NIFTY", before, universe_at(self.store, "NIFTY", before)), WEEKLY_NEXT)
        self.assertEqual(select_nearest_weekly_expiry("NIFTY", after, universe_at(self.store, "NIFTY", after)), WEEKLY_NEXT)
        self.assertNotIn(WEEKLY_SAME, {r.expiry for r in universe_at(self.store, "NIFTY", after)})

    def test_two_c_matches_helper_and_keeps_alternatives(self) -> None:
        src = HistoricalOptionSource(self.store)
        as_of = _ts(WEEKLY_SAME, 11, 0)
        chain = src.snapshot("NIFTY", as_of, spot=25000.0)
        visible = universe_at(self.store, "NIFTY", as_of)
        helper = select_nearest_weekly_expiry("NIFTY", as_of, visible)
        chosen, why = choose_expiry(chain, as_of, self.cfg)
        self.assertEqual(chosen.day, helper)
        self.assertTrue(why.startswith("EXPIRY:"))
        self.assertGreater(len(chain.expiries), 1)
        self.assertIn(MONTHLY, {e.day for e in chain.expiries})

    def test_deterministic_replay(self) -> None:
        a = build_eval_store()
        b = build_eval_store()
        self.assertEqual(a.meta.fingerprint, b.meta.fingerprint)
        as_of = _ts(date(2026, 9, 14), 11, 0)
        self.assertEqual(
            select_nearest_weekly_expiry("NIFTY", as_of, universe_at(a, "NIFTY", as_of)),
            select_nearest_weekly_expiry("NIFTY", as_of, universe_at(b, "NIFTY", as_of)),
        )

    def test_banknifty_ce_pe(self) -> None:
        as_of = _ts(date(2026, 9, 14), 11, 0)
        types = {c.option_type for c in self.store.contracts_at("BANKNIFTY", as_of)}
        self.assertEqual(types, {"CE", "PE"})
        self.assertEqual(select_nearest_weekly_expiry("BANKNIFTY", as_of, universe_at(self.store, "BANKNIFTY", as_of)), WEEKLY_SAME)


class PitLeakageTests(unittest.TestCase):
    def test_future_quote_and_contract_do_not_leak(self) -> None:
        store = build_eval_store(publish=False)
        as_of = _ts(date(2026, 9, 14), 11, 0)
        before_u = universe_at(store, "NIFTY", as_of)
        before_c, before_q = store.snapshot_quotes("NIFTY", as_of)
        future = datetime(2026, 9, 18, 15, 15, tzinfo=IST)
        store.add_contract(
            HistoricalOptionContract(
                underlying="NIFTY",
                expiry=date(2026, 10, 6),
                strike=25000,
                option_type="CE",
                contract_id="future-ce",
                provider_contract_id="future-ce",
                lot_size=75,
                expiry_class="WEEKLY",
                first_seen_at=future,
                last_seen_at=datetime(2026, 10, 6, 15, 30, tzinfo=IST),
                listing_status="ACTIVE",
                source_id=store.meta.source_id,
                dataset_version=store.meta.version,
            )
        )
        store.add_quote(
            HistoricalOptionQuote(
                contract_id="future-ce",
                timestamp=future,
                bid=10,
                ask=11,
                ltp=10.5,
                volume=1,
                open_interest=1,
                previous_open_interest=None,
                implied_volatility=0.2,
                delta=None,
                gamma=None,
                theta=None,
                vega=None,
                greek_source=None,
                iv_source="PROVIDER",
                source_id=store.meta.source_id,
                dataset_version=store.meta.version,
                as_of_available_at=future,
                quality_flags=(),
            )
        )
        after_c, after_q = store.snapshot_quotes("NIFTY", as_of)
        self.assertEqual([c.contract_id for c in before_c], [c.contract_id for c in after_c])
        self.assertEqual([q.ltp for q in before_q], [q.ltp for q in after_q])
        self.assertEqual({r.expiry for r in before_u}, {r.expiry for r in universe_at(store, "NIFTY", as_of)})
        self.assertNotIn("future-ce", [c.contract_id for c in after_c])
        later_c, _ = store.snapshot_quotes("NIFTY", future)
        self.assertIn("future-ce", [c.contract_id for c in later_c])


class HarnessAndVendorTests(unittest.TestCase):
    def test_eval_harness_qualifies_but_not_approved_for_2e(self) -> None:
        store = build_eval_store()
        result = ProviderEvaluationRunner().evaluate(store)
        by_name = {c.name: c.outcome for c in result.checks}
        self.assertEqual(by_name["PIT"], "PASS")
        self.assertEqual(by_name["NEAREST_WEEKLY"], "PASS")
        self.assertEqual(by_name["LOT_SIZE"], "PASS")
        self.assertEqual(by_name["CALENDAR"], "PASS")
        self.assertFalse(result.approved_for_2e)
        self.assertNotEqual(result.qualification_status, APPROVED_FOR_2E)
        self.assertEqual(store.meta.usage_scope, FRAMEWORK_TEST_ONLY)
        self.assertTrue(any("FRAMEWORK_TEST_ONLY" in item for item in result.limitations))

    def test_vendor_export_roundtrip_preserves_nearest_weekly(self) -> None:
        store = build_eval_store()
        payload = export_vendor_payload(store)
        self.assertEqual(payload["vendor_schema"], "grow.history.provider.export.v1")
        meta = replace(store.meta, fingerprint="pending")
        loaded = ingest_vendor_payload(payload, meta=meta)
        as_of = _ts(WEEKLY_SAME, 11, 0)
        self.assertEqual(
            select_nearest_weekly_expiry("NIFTY", as_of, universe_at(loaded, "NIFTY", as_of)),
            WEEKLY_NEXT,
        )
        with self.assertRaises(GrowConfigError) as ctx:
            ingest_vendor_payload({**payload, "mapping_policy": "NEAREST_WITHIN_TOLERANCE"}, meta=meta)
        self.assertIn("VENDOR_MAPPING_NOT_IMPLEMENTED", str(ctx.exception))

    def test_lot_size_and_calendar(self) -> None:
        store = build_eval_store()
        nifty = [c for c in store.all_contracts() if c.underlying == "NIFTY"]
        self.assertTrue(all(c.lot_size == 75 for c in nifty))
        self.assertEqual(store.session_on(date(2026, 9, 16)).special_reason, "high_volatility")
        snap = HistoricalMarketSource(store).snapshot("NIFTY", as_of=_ts(date(2026, 9, 14), 11, 0))
        self.assertEqual(snap.session, SessionState.OPEN)
        self.assertGreater(snap.last_price, 0)

    def test_2e_manifest_records_mapping(self) -> None:
        store = build_eval_store()
        man = build_manifest(
            load_config(),
            start=date(2026, 9, 14),
            end=date(2026, 9, 18),
            ablation="full",
            calendar_version=store.meta.calendar_version,
            dataset_id=EVAL_ID,
            dataset_version=store.meta.version,
            mapping_policy=store.meta.mapping_policy,
            slot_tolerance_seconds=store.meta.slot_tolerance_seconds,
            dataset_fingerprint=store.meta.fingerprint,
            provider_name=store.meta.provider_name,
        )
        self.assertEqual(man.mapping_policy, "EXACT")
        self.assertEqual(man.slot_tolerance_seconds, 0)
        self.assertEqual(man.dataset_fingerprint, store.meta.fingerprint)
        self.assertEqual(man.provider_name, "grow-eval-sample")

    def test_historical_manifest_requires_fingerprint(self) -> None:
        with self.assertRaises(GrowConfigError) as ctx:
            build_manifest(
                load_config(),
                start=date(2026, 9, 14),
                end=date(2026, 9, 18),
                ablation="full",
                dataset_id="vendor.nifty.v1",
                dataset_version="v1",
                provider_name="vendor",
                mapping_policy="EXACT",
                slot_tolerance_seconds=0,
            )
        self.assertIn("MISSING_DATASET_FINGERPRINT", str(ctx.exception))
        fixture = build_manifest(load_config(), start=date(2026, 9, 21), end=date(2026, 9, 21), ablation="full")
        self.assertEqual(fixture.provider_name, "fixture")
        self.assertEqual(fixture.dataset_fingerprint, "")

    def test_vendor_quote_fields_roundtrip(self) -> None:
        store = build_eval_store()
        before = [q.to_dict() for q in sorted(store.all_quotes(), key=lambda q: (q.contract_id, q.timestamp.isoformat()))]
        payload = export_vendor_payload(store)
        loaded = ingest_vendor_payload(payload, meta=replace(store.meta, fingerprint="pending"))
        after = [q.to_dict() for q in sorted(loaded.all_quotes(), key=lambda q: (q.contract_id, q.timestamp.isoformat()))]
        self.assertEqual(before, after)
        self.assertTrue(any(q["previous_open_interest"] is not None for q in after))
        self.assertTrue(any(q["iv_source"] == "PROVIDER" for q in after))
        self.assertTrue(all(q["as_of_available_at"] for q in after))

    def test_derived_warnings_ignore_empty_meta_list(self) -> None:
        store = CanonicalStore(
            _meta(
                instrument_scope=("NIFTY", "BANKNIFTY"),
                snapshot_cadence=("11:00", "15:15"),
                quality_warnings=(),
                iv_available=False,
            )
        )
        store.add_session(_session())
        first = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
        last = datetime(2026, 9, 22, 15, 30, tzinfo=IST)
        ts = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
        for symbol in ("NIFTY", "BANKNIFTY"):
            for kind in ("CE", "PE"):
                cid = f"{symbol}-25000-{kind}"
                store.add_contract(
                    HistoricalOptionContract(
                        underlying=symbol,
                        expiry=date(2026, 9, 22),
                        strike=25000,
                        option_type=kind,
                        contract_id=cid,
                        provider_contract_id=cid,
                        lot_size=75,
                        expiry_class="WEEKLY",
                        first_seen_at=first,
                        last_seen_at=last,
                        listing_status="ACTIVE",
                        source_id="t",
                        dataset_version="v1",
                    )
                )
                store.add_quote(
                    HistoricalOptionQuote(
                        contract_id=cid,
                        timestamp=ts,
                        bid=10,
                        ask=11,
                        ltp=10.5,
                        volume=1,
                        open_interest=1,
                        previous_open_interest=None,
                        implied_volatility=None,
                        delta=None,
                        gamma=None,
                        theta=None,
                        vega=None,
                        greek_source=None,
                        iv_source=None,
                        source_id="t",
                        dataset_version="v1",
                        as_of_available_at=ts,
                        quality_flags=(),
                    )
                )
        self.assertEqual(store.meta.quality_warnings, ())
        self.assertIn(OPTION_SNAPSHOT_GAPS, derive_quality_warnings(store))
        store.publish()
        result = ProviderEvaluationRunner().evaluate(store)
        self.assertIn(OPTION_SNAPSHOT_GAPS, result.quality_warnings)

    def test_qualification_record_does_not_mutate_store(self) -> None:
        store = build_eval_store()
        self.assertEqual(store.meta.qualification_status, CANDIDATE)
        record = ProviderEvaluationRunner().qualify(store)
        self.assertEqual(store.meta.qualification_status, CANDIDATE)
        self.assertEqual(record.prior_status, CANDIDATE)
        self.assertFalse(record.approved_for_2e)
        self.assertNotEqual(record.qualification_status, APPROVED_FOR_2E)
        fixture_src = default_catalog()[SAMPLE_ID]
        with self.assertRaises(GrowConfigError):
            consume_qualification(
                fixture_src,
                replace(
                    record,
                    dataset_id=SAMPLE_ID,
                    dataset_version=fixture_src.dataset_version,
                    fingerprint=fixture_src.fingerprint,
                    approved_for_2e=True,
                    qualification_status=APPROVED_FOR_2E,
                ),
            )
        updated = consume_qualification(
            replace(
                fixture_src,
                dataset_id=record.dataset_id,
                dataset_version=record.dataset_version,
                usage_scope="HISTORICAL_RESEARCH",
                is_fixture=False,
                licensing_status="APPROVED",
                quality_status="APPROVED",
                fingerprint=record.fingerprint,
            ),
            record,
        )
        self.assertEqual(updated.qualification_status, record.qualification_status)
        self.assertNotEqual(updated.qualification_status, APPROVED_FOR_2E)
        with self.assertRaises(GrowConfigError) as ctx:
            require_approved_for_2e({updated.dataset_id: updated}, updated.dataset_id)
        self.assertIn("NOT_APPROVED_FOR_2E", str(ctx.exception))

    def test_qualification_fingerprint_and_approval_binding(self) -> None:
        store = build_eval_store()
        record = ProviderEvaluationRunner().qualify(store)
        src = ApprovedDataSource(
            dataset_id=record.dataset_id,
            provider="file",
            instrument_scope=("NIFTY", "BANKNIFTY"),
            date_coverage=(date(2026, 1, 1), date(2026, 12, 31)),
            timestamp_granularity=("M15",),
            timezone="Asia/Kolkata",
            option_chain_depth="atm_pm2",
            bid_ask_available=True,
            oi_available=True,
            volume_available=True,
            iv_available=False,
            greeks_available=False,
            historical_contract_metadata=True,
            session_calendar_version="nse.session.eval.v1",
            quality_status="APPROVED",
            licensing_status="APPROVED",
            dataset_version=record.dataset_version,
            provenance="test",
            usage_scope="HISTORICAL_RESEARCH",
            is_fixture=False,
            fingerprint=record.fingerprint,
        )
        consume_qualification(src, record)
        with self.assertRaises(GrowConfigError) as ctx:
            consume_qualification(replace(src, fingerprint="deadbeef" * 8), record)
        self.assertIn("QUALIFICATION_FINGERPRINT_MISMATCH", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            consume_qualification(src, replace(record, approved_for_2e=True))
        self.assertIn("QUALIFICATION_APPROVAL_INCONSISTENT", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            consume_qualification(src, replace(record, schema="dataset.qualification.v0"))
        self.assertIn("QUALIFICATION_SCHEMA", str(ctx.exception))

    def test_adversarial_pit_leaves_original_store_unchanged(self) -> None:
        store = build_eval_store()
        n = len(store.all_contracts())
        from grow.history.eval import _pit

        check = _pit(store)
        self.assertEqual(check.outcome, "PASS")
        self.assertEqual(len(store.all_contracts()), n)
        self.assertFalse(any(c.contract_id == "pit-adversarial-future-ce" for c in store.all_contracts()))

    def test_2f_rejects_framework_sample_for_2e(self) -> None:
        cat = default_catalog()
        with self.assertRaises(GrowConfigError) as ctx:
            require_approved_for_2e(cat, SAMPLE_ID)
        self.assertIn("DATASET", str(ctx.exception))
        _, plan = _valid_plan()
        hist = ApprovedDataSource(
            dataset_id="hist.2e",
            provider="file",
            instrument_scope=("NIFTY", "BANKNIFTY"),
            date_coverage=(date(2026, 1, 1), date(2026, 12, 31)),
            timestamp_granularity=("M5", "M15", "D1"),
            timezone="Asia/Kolkata",
            option_chain_depth="atm_pm2",
            bid_ask_available=True,
            oi_available=True,
            volume_available=True,
            iv_available=False,
            greeks_available=False,
            historical_contract_metadata=True,
            session_calendar_version=plan.session_calendar_version,
            quality_status="APPROVED",
            licensing_status="APPROVED",
            dataset_version="v1",
            provenance="test",
            usage_scope="HISTORICAL_RESEARCH",
            is_fixture=False,
            qualification_status=APPROVED_FOR_2E,
        )
        cat = {**cat, hist.dataset_id: hist}
        require_approved_for_2e(cat, hist.dataset_id)
        ResearchPlanValidator().validate(replace(plan, dataset_id=hist.dataset_id, dataset_version="v1"), cat)


if __name__ == "__main__":
    unittest.main()
