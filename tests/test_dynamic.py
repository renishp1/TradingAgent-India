from __future__ import annotations

import unittest
from datetime import date, datetime
from dataclasses import replace

from grow.clock import IST
from grow.config import load_config
from grow.dashboard import provider_evaluation_view
from grow.data.schema import Timeframe
from grow.errors import GrowConfigError
from grow.history.candidate_flow import DynamicCandidateOrchestrator
from grow.history.candidates import PUBLIC_CANDIDATES
from grow.history.eval import ProviderEvaluationRunner
from grow.history.expiry import select_nearest_weekly_expiry, universe_at
from grow.history.qualify_store import DatasetQualificationStore
from grow.history.resolver import NO_ELIGIBLE_EXPIRY, SAME_DAY_FORBIDDEN, resolve_nearest_expiry
from grow.history.sample_2i import (
    MIDCP_MONTHLY,
    NIFTY_WEEKLY_NEAR,
    NIFTY_WEEKLY_NEXT,
    SAMPLE_2I_ID,
    build_2i_store,
    trading_days,
)
from grow.history.scorecard import scorecard_from
from grow.history.universe import MONTHLY_ONLY, WEEKLY_PREFERRED, default_index_registry, discover_underlyings
from grow.options.engine import IndexOptionsEngine
from grow.options.select import choose_expiry
from grow.strategies.signal import StrategySignal
from grow.types import Symbol


def _ts(day: date, hour: int = 11, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def _signal(ticker: str, direction: str, as_of: datetime) -> StrategySignal:
    return StrategySignal(
        symbol=Symbol(ticker),
        strategy="ema_trend",
        direction=direction,
        entry=100.0,
        stop=90.0 if direction == "BULLISH" else 110.0,
        target=120.0 if direction == "BULLISH" else 80.0,
        confidence=0.6,
        timeframe=Timeframe.M15,
        reason="test",
        as_of=as_of,
        snapshot_id="pending",
        signal_id=f"sig-{ticker}",
        strategy_version="v1",
    )


class DynamicUniverseTests(unittest.TestCase):
    def test_registry_is_not_nifty_only(self) -> None:
        reg = default_index_registry()
        self.assertTrue(reg.allows("NIFTY"))
        self.assertTrue(reg.allows("BANKNIFTY"))
        self.assertTrue(reg.allows("MIDCPNIFTY"))
        self.assertFalse(reg.allows("RELIANCE"))
        self.assertEqual(reg.policy("NIFTY").expiry_policy_profile, WEEKLY_PREFERRED)
        self.assertEqual(reg.policy("MIDCPNIFTY").expiry_policy_profile, MONTHLY_ONLY)

    def test_discovery_respects_listing_and_excludes_stocks(self) -> None:
        store = build_2i_store()
        as_of = _ts(date(2026, 9, 14))
        found = discover_underlyings(store, as_of)
        by_id = {row.canonical_symbol: row for row in found}
        self.assertEqual(by_id["NIFTY"].status, "ELIGIBLE")
        self.assertEqual(by_id["BANKNIFTY"].status, "ELIGIBLE")
        self.assertEqual(by_id["MIDCPNIFTY"].status, "ELIGIBLE")
        self.assertNotIn("RELIANCE", by_id)
        self.assertTrue(all(row.status != "ELIGIBLE" or row.canonical_symbol in store.meta.instrument_scope for row in found))

    def test_ten_sessions(self) -> None:
        self.assertEqual(len(trading_days()), 10)
        store = build_2i_store()
        opens = [s.session_date for s in store._sessions.values() if s.status == "OPEN"]
        self.assertEqual(len(opens), 10)


class ExpiryResolverTests(unittest.TestCase):
    def test_weekly_skips_same_day_and_matches_2c(self) -> None:
        store = build_2i_store()
        as_of = _ts(NIFTY_WEEKLY_NEAR)
        visible = universe_at(store, "NIFTY", as_of)
        weekly = select_nearest_weekly_expiry("NIFTY", as_of, visible)
        self.assertEqual(weekly, NIFTY_WEEKLY_NEXT)
        resolved = resolve_nearest_expiry("NIFTY", as_of, visible, WEEKLY_PREFERRED)
        self.assertEqual(resolved.selected_expiry, NIFTY_WEEKLY_NEXT.isoformat())
        self.assertTrue(any(SAME_DAY_FORBIDDEN in item for item in resolved.exclusion_reasons))
        from grow.history.bridge import HistoricalOptionSource, HistoricalMarketSource

        snap = HistoricalMarketSource(store).snapshot("NIFTY", as_of)
        chain = HistoricalOptionSource(store).snapshot("NIFTY", as_of, spot=snap.last_price)
        chosen, _ = choose_expiry(chain, as_of, load_config().options, policy_profile=WEEKLY_PREFERRED)
        self.assertEqual(chosen.day, NIFTY_WEEKLY_NEXT)

    def test_monthly_only_midcpnifty(self) -> None:
        store = build_2i_store()
        as_of = _ts(date(2026, 9, 14))
        visible = universe_at(store, "MIDCPNIFTY", as_of)
        weekly = select_nearest_weekly_expiry("MIDCPNIFTY", as_of, visible)
        self.assertIsNone(weekly)
        resolved = resolve_nearest_expiry("MIDCPNIFTY", as_of, visible, MONTHLY_ONLY)
        self.assertEqual(resolved.selected_expiry, MIDCP_MONTHLY.isoformat())
        self.assertEqual(resolved.selected_expiry_class, "MONTHLY")

    def test_no_eligible_expiry(self) -> None:
        store = build_2i_store()
        as_of = _ts(date(2026, 9, 14))
        resolved = resolve_nearest_expiry("NIFTY", as_of, (), WEEKLY_PREFERRED)
        self.assertIsNone(resolved.selected_expiry)
        self.assertIn(NO_ELIGIBLE_EXPIRY, resolved.exclusion_reasons)

    def test_future_listed_weekly_absent_before_listing(self) -> None:
        store = build_2i_store()
        as_of = _ts(date(2026, 9, 14))
        visible = {r.expiry for r in universe_at(store, "NIFTY", as_of)}
        self.assertNotIn(date(2026, 9, 22), visible)
        later = {r.expiry for r in universe_at(store, "NIFTY", _ts(date(2026, 9, 16)))}
        self.assertIn(date(2026, 9, 22), later)


class QualificationAndFlowTests(unittest.TestCase):
    def test_2i_harness_and_no_approved_for_2e(self) -> None:
        store = build_2i_store()
        result = ProviderEvaluationRunner().evaluate(store)
        by_name = {c.name: c.outcome for c in result.checks}
        self.assertEqual(by_name["PIT"], "PASS")
        self.assertEqual(by_name["UNIVERSE"], "PASS")
        self.assertEqual(by_name["INDEX_DISCOVERY"], "PASS")
        self.assertEqual(by_name["NEAREST_WEEKLY"], "PASS")
        self.assertFalse(result.approved_for_2e)
        self.assertNotEqual(result.qualification_status, "APPROVED_FOR_2E")
        card = scorecard_from(store, result)
        self.assertIn("MIDCPNIFTY", card.supported_indices)
        self.assertEqual(card.label, "HISTORICAL RESEARCH / NOT LIVE")
        self.assertFalse(card.live)
        view = provider_evaluation_view(store, result)
        self.assertEqual(len(view["public_candidates"]), 3)
        self.assertTrue(all(c["status"] == "CANDIDATE" for c in view["public_candidates"]))

    def test_qualification_store_fingerprint_bound(self) -> None:
        store = build_2i_store()
        record = ProviderEvaluationRunner().qualify(store)
        bag = DatasetQualificationStore()
        bag.put(record)
        self.assertEqual(bag.get(record.dataset_id, record.dataset_version, record.fingerprint).fingerprint, record.fingerprint)
        with self.assertRaises(GrowConfigError):
            bag.get(record.dataset_id, record.dataset_version, "deadbeef")

    def test_public_candidates_are_not_approved(self) -> None:
        self.assertEqual(len(PUBLIC_CANDIDATES), 3)
        self.assertTrue(all(c.status == "CANDIDATE" and not c.live for c in PUBLIC_CANDIDATES))

    def test_candidate_flow_cannot_invent_symbol(self) -> None:
        store = build_2i_store()
        as_of = _ts(date(2026, 9, 14))
        flow = DynamicCandidateOrchestrator(store)
        invented = _signal("RELIANCE", "BULLISH", as_of)
        result = flow.evaluate(as_of, {"RELIANCE": invented, "NIFTY": _signal("NIFTY", "BULLISH", as_of)})
        symbols = {row.underlying for row in result.contexts}
        self.assertNotIn("RELIANCE", symbols)
        self.assertIn("NIFTY", symbols)
        for row in result.contexts:
            if row.decision.candidate is not None:
                self.assertEqual(row.decision.candidate.underlying, row.underlying)
                self.assertIn(row.decision.candidate.option_type, {"CE", "PE"})

    def test_engine_monthly_profile_for_midcpnifty(self) -> None:
        store = build_2i_store()
        as_of = _ts(date(2026, 9, 14))
        from grow.history.bridge import HistoricalMarketSource, HistoricalOptionSource

        snap = HistoricalMarketSource(store).snapshot("MIDCPNIFTY", as_of)
        chain = HistoricalOptionSource(store).snapshot("MIDCPNIFTY", as_of, spot=snap.last_price)
        signal = replace(_signal("MIDCPNIFTY", "BEARISH", as_of), snapshot_id=snap.snapshot_id, as_of=snap.as_of)
        decision = IndexOptionsEngine(load_config()).evaluate(signal, snap, chain)
        if decision.candidate is not None:
            self.assertEqual(decision.candidate.option_type, "PE")
            self.assertEqual(decision.candidate.expiry, MIDCP_MONTHLY)

    def test_repeated_eval_is_deterministic(self) -> None:
        store = build_2i_store()
        a = ProviderEvaluationRunner().evaluate(store)
        b = ProviderEvaluationRunner().evaluate(store)
        self.assertEqual(a.to_dict(), b.to_dict())
        as_of = _ts(date(2026, 9, 14))
        vis = universe_at(store, "NIFTY", as_of)
        r1 = resolve_nearest_expiry("NIFTY", as_of, vis, WEEKLY_PREFERRED)
        r2 = resolve_nearest_expiry("NIFTY", as_of, vis, WEEKLY_PREFERRED)
        self.assertEqual(r1.resolution_id, r2.resolution_id)

    def test_new_optidx_is_discoverable_via_policy_overlay(self) -> None:
        from grow.history.models import HistoricalOptionContract
        from grow.history.store import CanonicalStore
        from grow.history.universe import IndexUniverseRegistry, _policy, default_index_policies
        from tests.test_history import _meta, _session

        store = CanonicalStore(_meta(instrument_scope=("FINNIFTY",)))
        store.add_session(_session())
        first = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
        last = datetime(2026, 9, 29, 15, 30, tzinfo=IST)
        store.add_contract(
            HistoricalOptionContract(
                underlying="FINNIFTY",
                expiry=date(2026, 9, 29),
                strike=25000,
                option_type="CE",
                contract_id="FINNIFTY-25000-CE",
                provider_contract_id="FINNIFTY-25000-CE",
                lot_size=40,
                expiry_class="WEEKLY",
                first_seen_at=first,
                last_seen_at=last,
                listing_status="ACTIVE",
                source_id="t",
                dataset_version="v1",
            )
        )
        as_of = _ts(date(2026, 9, 21))
        unauthorized = {r.canonical_symbol: r for r in discover_underlyings(store, as_of)}
        self.assertEqual(unauthorized["FINNIFTY"].status, "UNAUTHORIZED")
        overlay = IndexUniverseRegistry(
            default_index_policies()
            + (_policy("FINNIFTY", name="Nifty Financial", profile=WEEKLY_PREFERRED, active_from=date(2021, 1, 1)),)
        )
        approved = {r.canonical_symbol: r for r in discover_underlyings(store, as_of, overlay)}
        self.assertEqual(approved["FINNIFTY"].status, "ELIGIBLE")
        self.assertEqual(approved["FINNIFTY"].expiry_policy_profile, WEEKLY_PREFERRED)

    def test_custom_historical_and_unknown_profiles_fail_closed(self) -> None:
        store = build_2i_store()
        as_of = _ts(date(2026, 9, 14))
        vis = universe_at(store, "NIFTY", as_of)
        with self.assertRaises(GrowConfigError) as ctx:
            resolve_nearest_expiry("NIFTY", as_of, vis, "CUSTOM_HISTORICAL")
        self.assertIn("CUSTOM_HISTORICAL_UNIMPLEMENTED", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            resolve_nearest_expiry("NIFTY", as_of, vis, "SILENT_WEEKLY")
        self.assertIn("UNKNOWN_EXPIRY_PROFILE", str(ctx.exception))
        from grow.history.bridge import HistoricalMarketSource, HistoricalOptionSource

        snap = HistoricalMarketSource(store).snapshot("NIFTY", as_of)
        chain = HistoricalOptionSource(store).snapshot("NIFTY", as_of, spot=snap.last_price)
        none, why = choose_expiry(chain, as_of, load_config().options, policy_profile="CUSTOM_HISTORICAL")
        self.assertIsNone(none)
        self.assertEqual(why, "CUSTOM_HISTORICAL_UNIMPLEMENTED")
        none, why = choose_expiry(chain, as_of, load_config().options, policy_profile="NOT_A_PROFILE")
        self.assertEqual(why, "UNKNOWN_EXPIRY_PROFILE:NOT_A_PROFILE")

    def test_historical_provider_mode_and_live_blocked(self) -> None:
        cfg = replace(load_config(), options=replace(load_config().options, provider="historical"))
        cfg.assert_safe()
        engine = IndexOptionsEngine(cfg)
        self.assertEqual(engine.config.options.provider, "historical")
        live = replace(cfg, options=replace(cfg.options, provider="live"))
        with self.assertRaises(GrowConfigError):
            live.assert_safe()
        with self.assertRaises(GrowConfigError):
            IndexOptionsEngine(live)
        store = build_2i_store()
        store.meta = replace(store.meta, is_fixture=False)
        flow = DynamicCandidateOrchestrator(store)
        self.assertEqual(flow.engine.config.options.provider, "historical")
        as_of = _ts(date(2026, 9, 14))
        result = flow.evaluate(as_of, {"NIFTY": _signal("NIFTY", "BULLISH", as_of)})
        self.assertTrue(any(row.underlying == "NIFTY" for row in result.contexts))

    def test_expiry_resolution_mismatch_rewrites_no_trade(self) -> None:
        from grow.history.candidate_flow import EXPIRY_RESOLUTION_MISMATCH, bind_resolution
        from grow.history.resolver import ExpiryResolution
        from grow.options.fixture import FixtureOptionChain
        from tests.test_options_engine import _signal as opt_signal, _snapshot

        as_of = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
        snap = _snapshot("NIFTY", as_of, 25000.0)
        chain = FixtureOptionChain().snapshot("NIFTY", as_of, spot=25000.0)
        signal = replace(opt_signal("NIFTY", "BULLISH", as_of, snap.snapshot_id), as_of=snap.as_of)
        decision = IndexOptionsEngine(load_config()).evaluate(signal, snap, chain)
        self.assertIsNotNone(decision.candidate)
        wrong = ExpiryResolution(
            resolution_id="x",
            underlying="NIFTY",
            as_of=as_of.isoformat(),
            discovered_expiries=(),
            excluded_expiries=(),
            policy_profile=WEEKLY_PREFERRED,
            selected_expiry="1999-01-01",
            selected_expiry_class="WEEKLY",
            exclusion_reasons=(),
            dataset_id="",
            dataset_version="",
            dataset_fingerprint="",
        )
        bound = bind_resolution(decision, wrong)
        self.assertIsNone(bound.candidate)
        self.assertEqual(bound.status.value, "NO_TRADE")
        self.assertIn(EXPIRY_RESOLUTION_MISMATCH, bound.diagnostics)
        ok = bind_resolution(decision, replace(wrong, selected_expiry=decision.candidate.expiry.isoformat()))
        self.assertIsNotNone(ok.candidate)

    def test_candidateset_research_handoff_is_immutable(self) -> None:
        store = build_2i_store()
        as_of = _ts(date(2026, 9, 14))
        result = DynamicCandidateOrchestrator(store).evaluate(
            as_of, {"NIFTY": _signal("NIFTY", "BULLISH", as_of)}
        )
        payload = result.to_research_input()
        self.assertEqual(payload["schema"], "research.candidateset.v1")
        with self.assertRaises(TypeError):
            payload["schema"] = "hacked"  # type: ignore[index]
        none = result.pick(None)
        self.assertEqual(none["verdict"], "NO_TRADE")
        unknown = result.pick("not-a-candidate")
        self.assertEqual(unknown["reason"], "UNKNOWN_CANDIDATE")
        if result.candidates():
            cid = result.candidates()[0].decision.candidate.candidate_id
            picked = result.pick(cid)
            self.assertEqual(picked["verdict"], "TRADE_APPROVE")
            self.assertEqual(picked["candidate"]["candidate_id"], cid)
            with self.assertRaises(TypeError):
                picked["candidate"] = {}  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()

