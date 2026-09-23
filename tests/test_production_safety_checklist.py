"""Production safety checklist + Zerodha live-proof outcome tests."""

from __future__ import annotations

import json
import unittest
from datetime import timedelta

from grow.clock import FrozenClock
from grow.errors import GrowConfigError
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.live_data.kite_market import KiteMarketProvider, ScriptedKiteTransport
from grow.live_data.smoke import kite_market_smoke_config
from grow.market.session import SessionCalendar
from grow.safety import (
    CHECKLIST_SCHEMA,
    LIVE_PROOF_IDS,
    LiveProofHooks,
    ProductionSafetyReport,
    run_production_safety_checklist,
    run_zerodha_live_proofs,
)
from grow.safety.checklist import ChecklistItem
from grow.safety.live_proofs import (
    _prove_side_tick,
    _prove_websocket_reconnect,
    assert_live_proofs_forbid_broker_orders,
)
from tests.test_live_data import AS_OF
from tests.test_zerodha_market import CE_TOKEN, CSV, FAR_TOKEN, PE_TOKEN, _frame, _full_packet


def _ce_pe_frames(when):
    """Initial CE/PE plus distinct post-reconnect ticks (different LTP)."""
    return [
        _frame(_full_packet(CE_TOKEN, ltp=101.5, bid=101.0, ask=102.0, volume=40, oi=80, when=when)),
        _frame(_full_packet(PE_TOKEN, ltp=88.0, bid=87.5, ask=88.5, volume=12, oi=30, when=when)),
        _frame(_full_packet(CE_TOKEN, ltp=102.0, bid=101.5, ask=102.5, volume=41, oi=81, when=when)),
        _frame(_full_packet(PE_TOKEN, ltp=89.0, bid=88.5, ask=89.5, volume=13, oi=31, when=when)),
    ]


def _open_scripted(frames, *, clock: FrozenClock):
    def factory(api_key: str, access_token: str, _clock):
        assert api_key and access_token
        transport = ScriptedKiteTransport(
            instruments_csv=CSV,
            spots={"NIFTY": 24210.0},
            frames=list(frames),
        )
        provider = KiteMarketProvider(transport=transport, clock=clock)
        provider.connect()
        return provider

    return factory


def _connected_provider(frames, *, clock: FrozenClock) -> KiteMarketProvider:
    transport = ScriptedKiteTransport(
        instruments_csv=CSV,
        spots={"NIFTY": 24210.0},
        frames=list(frames),
    )
    provider = KiteMarketProvider(transport=transport, clock=clock)
    provider.connect()
    return provider


def _calendar(clock: FrozenClock) -> SessionCalendar:
    cfg = kite_market_smoke_config(
        {
            "ZERODHA_SMOKE": "1",
            "GROW_RISK_SECRET": "unit-test-risk-secret-value",
        }
    )
    return SessionCalendar(cfg.market, clock=clock)


def _usable_quote(*, option_type: str, token: int, symbol: str, strike: float, when, ltp: float = 100.0):
    return {
        "option_type": option_type,
        "underlying": "NIFTY",
        "expiry": "2026-09-22",
        "strike": strike,
        "provider_symbol": symbol,
        "provider_symbol_id": str(token),
        "canonical_id": f"NIFTY-2026-09-22-{int(strike)}-{option_type}",
        "ltp": ltp,
        "bid": ltp - 0.5,
        "ask": ltp + 0.5,
        "quote_timestamp": when.isoformat(),
        "is_fixture": False,
    }


CRED_ENV = {
    "KITE_API_KEY": "test-key",
    "KITE_ACCESS_TOKEN": "test-token",
    "ZERODHA_SMOKE": "1",
    "GROW_RISK_SECRET": "unit-test-risk-secret-value",
}


class ProductionSafetyChecklistTests(unittest.TestCase):
    def test_checklist_runs_and_reports_schema(self):
        report = run_production_safety_checklist(environ={})
        self.assertIsInstance(report, ProductionSafetyReport)
        payload = report.to_dict()
        self.assertEqual(payload["schema"], CHECKLIST_SCHEMA)
        self.assertFalse(payload["live_trading_qualification_ready"])
        self.assertTrue(payload["paper_mode"])
        self.assertFalse(payload["live_trading"])
        self.assertFalse(payload["broker_order_path"])
        self.assertEqual(report.passed + report.failed + report.blocked, len(report.items))
        json.dumps(payload)

    def test_compile_lock_and_paper_invariants_pass(self):
        report = run_production_safety_checklist(environ={})
        by_id = {item.id: item for item in report.items}
        self.assertFalse(LIVE_TRADING_COMPILED)
        self.assertEqual(by_id["paper_mode_compile_lock"].status, "PASS")
        self.assertEqual(by_id["live_trading_false"].status, "PASS")
        self.assertEqual(by_id["paper_mode_true"].status, "PASS")
        self.assertEqual(by_id["no_broker_order_calls"].status, "PASS")
        self.assertEqual(by_id["broker_order_path_false"].status, "PASS")
        self.assertEqual(by_id["buyer_only_no_option_selling"].status, "PASS")
        self.assertEqual(by_id["no_short_positions"].status, "PASS")
        self.assertEqual(by_id["risk_guard_final_authority"].status, "PASS")
        self.assertEqual(by_id["india_10k_profile_verified"].status, "PASS")

    def test_auth_missing_blocks_all_live_proofs(self):
        report = run_production_safety_checklist(environ={})
        by_id = {item.id: item for item in report.items}
        for item_id in LIVE_PROOF_IDS:
            self.assertEqual(by_id[item_id].status, "BLOCKED", item_id)
            self.assertIn("AUTH_MISSING", by_id[item_id].detail + by_id[item_id].evidence)
        self.assertEqual(report.blocked, 4)
        self.assertEqual(report.failed, 0)
        self.assertFalse(report.live_trading_qualification_ready)

    def test_phase12_phase13_surfaces_present(self):
        report = run_production_safety_checklist(environ={})
        by_id = {item.id: item for item in report.items}
        self.assertEqual(by_id["historical_validation_pit"].status, "PASS")
        self.assertEqual(by_id["paper_campaign_infrastructure"].status, "PASS")
        self.assertEqual(by_id["evaluation_labels"].status, "PASS")
        self.assertEqual(by_id["complete_audit_journal"].status, "PASS")
        self.assertEqual(by_id["deterministic_replay"].status, "PASS")

    def test_no_failed_items_when_credentials_missing(self):
        report = run_production_safety_checklist(environ={})
        failed = [item for item in report.items if item.status == "FAIL"]
        self.assertEqual(
            failed,
            [],
            msg="; ".join(f"{item.id}: {item.detail}" for item in failed),
        )

    def test_item_to_dict_shape(self):
        item = ChecklistItem("x", "PASS", "detail", "evidence")
        self.assertEqual(
            item.to_dict(),
            {"id": "x", "status": "PASS", "detail": "detail", "evidence": "evidence"},
        )

    def test_live_proofs_module_forbids_broker_order_calls(self):
        assert_live_proofs_forbid_broker_orders()


class LiveProofOutcomeTests(unittest.TestCase):
    def test_mocked_success_passes_all_four_proofs(self):
        when = AS_OF - timedelta(seconds=2)
        clock = FrozenClock(AS_OF)
        frames = _ce_pe_frames(when)
        hooks = LiveProofHooks(
            open_provider=_open_scripted(frames, clock=clock),
            poll_attempts=8,
            reconnect_wait_seconds=1.0,
        )
        results = run_zerodha_live_proofs(environ=CRED_ENV, hooks=hooks, clock=clock)
        by_id = {item.id: item for item in results}
        for item_id in LIVE_PROOF_IDS:
            self.assertEqual(by_id[item_id].status, "PASS", f"{item_id}: {by_id[item_id].detail}")
        self.assertIn("selected_contract=", by_id["real_zerodha_ce_tick"].evidence)
        self.assertIn("received_token=", by_id["real_zerodha_ce_tick"].evidence)
        self.assertIn(str(CE_TOKEN), by_id["real_zerodha_ce_tick"].evidence)
        self.assertIn(str(PE_TOKEN), by_id["real_zerodha_pe_tick"].evidence)
        self.assertIn("reconnect_count=", by_id["websocket_reconnect_live_proof"].evidence)
        self.assertIn("post_reconnect_quote=", by_id["websocket_reconnect_live_proof"].evidence)
        self.assertIn("chain_contracts=", by_id["real_option_chain_live_proof"].evidence)

    def test_credentials_present_but_no_ticks_is_fail_not_blocked(self):
        clock = FrozenClock(AS_OF)
        hooks = LiveProofHooks(
            open_provider=_open_scripted([], clock=clock),
            poll_attempts=4,
            reconnect_wait_seconds=1.0,
        )
        results = run_zerodha_live_proofs(environ=CRED_ENV, hooks=hooks, clock=clock)
        by_id = {item.id: item for item in results}
        self.assertEqual(by_id["real_zerodha_ce_tick"].status, "FAIL")
        self.assertEqual(by_id["real_zerodha_pe_tick"].status, "FAIL")
        self.assertNotEqual(by_id["real_zerodha_ce_tick"].status, "BLOCKED")
        self.assertNotIn("AUTH_MISSING", by_id["real_zerodha_ce_tick"].detail)
        # Chain can still validate from catalog after connect.
        self.assertIn(by_id["real_option_chain_live_proof"].status, {"PASS", "FAIL"})

    def test_connect_operational_failure_is_fail(self):
        def boom(api_key, access_token, clock):
            raise GrowConfigError("METADATA_UNAVAILABLE")

        hooks = LiveProofHooks(open_provider=boom)
        results = run_zerodha_live_proofs(environ=CRED_ENV, hooks=hooks, clock=FrozenClock(AS_OF))
        self.assertTrue(all(item.status == "FAIL" for item in results))
        self.assertTrue(all("METADATA_UNAVAILABLE" in item.detail for item in results))
        self.assertTrue(all(item.status != "BLOCKED" for item in results))

    def test_auth_failed_after_credentials_is_fail(self):
        def boom(api_key, access_token, clock):
            raise GrowConfigError("AUTH_FAILED")

        hooks = LiveProofHooks(open_provider=boom)
        results = run_zerodha_live_proofs(environ=CRED_ENV, hooks=hooks, clock=FrozenClock(AS_OF))
        self.assertTrue(all(item.status == "FAIL" for item in results))
        self.assertTrue(all("AUTH_FAILED" in item.detail for item in results))

    def test_only_ce_tick_fails_pe_proof(self):
        when = AS_OF - timedelta(seconds=2)
        clock = FrozenClock(AS_OF)
        frames = [
            _frame(_full_packet(CE_TOKEN, ltp=101.5, bid=101.0, ask=102.0, volume=40, oi=80, when=when)),
        ]
        hooks = LiveProofHooks(
            open_provider=_open_scripted(frames, clock=clock),
            poll_attempts=4,
            reconnect_wait_seconds=1.0,
        )
        results = run_zerodha_live_proofs(environ=CRED_ENV, hooks=hooks, clock=clock)
        by_id = {item.id: item for item in results}
        self.assertEqual(by_id["real_zerodha_ce_tick"].status, "PASS")
        self.assertEqual(by_id["real_zerodha_pe_tick"].status, "FAIL")
        self.assertEqual(by_id["real_option_chain_live_proof"].status, "PASS")

    def test_qualification_ready_false_when_any_live_proof_blocked(self):
        report = run_production_safety_checklist(environ={})
        self.assertEqual(report.blocked, 4)
        self.assertFalse(report.live_trading_qualification_ready)
        self.assertFalse(report.to_dict()["live_trading_qualification_ready"])

    def test_qualification_ready_false_when_any_live_proof_fails(self):
        clock = FrozenClock(AS_OF)
        hooks = LiveProofHooks(
            open_provider=_open_scripted([], clock=clock),
            poll_attempts=2,
            reconnect_wait_seconds=1.0,
        )
        report = run_production_safety_checklist(environ=CRED_ENV, live_proof_hooks=hooks)
        self.assertGreater(report.failed, 0)
        self.assertFalse(report.live_trading_qualification_ready)

    def test_qualification_ready_true_only_when_all_live_proofs_pass(self):
        when = AS_OF - timedelta(seconds=2)
        clock = FrozenClock(AS_OF)
        frames = _ce_pe_frames(when)
        hooks = LiveProofHooks(
            open_provider=_open_scripted(frames, clock=clock),
            poll_attempts=8,
            reconnect_wait_seconds=1.0,
        )
        report = run_production_safety_checklist(environ=CRED_ENV, live_proof_hooks=hooks)
        by_id = {item.id: item for item in report.items}
        for item_id in LIVE_PROOF_IDS:
            self.assertEqual(by_id[item_id].status, "PASS", item_id)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.blocked, 0)
        self.assertTrue(report.live_trading_qualification_ready)
        self.assertTrue(report.to_dict()["live_trading_qualification_ready"])

    def test_credentials_alone_do_not_count_as_proof(self):
        """Secrets present with empty feed → FAIL, never PASS from credentials alone."""
        clock = FrozenClock(AS_OF)
        hooks = LiveProofHooks(
            open_provider=_open_scripted([], clock=clock),
            poll_attempts=2,
            reconnect_wait_seconds=1.0,
        )
        results = run_zerodha_live_proofs(environ=CRED_ENV, hooks=hooks, clock=clock)
        tick_statuses = {
            item.status
            for item in results
            if item.id in {"real_zerodha_ce_tick", "real_zerodha_pe_tick"}
        }
        self.assertEqual(tick_statuses, {"FAIL"})


class LiveProofIdentityAndReconnectTests(unittest.TestCase):
    def test_different_ce_tick_fails_selected_identity(self):
        when = AS_OF - timedelta(seconds=2)
        clock = FrozenClock(AS_OF)
        provider = _connected_provider([], clock=clock)
        wrong = _usable_quote(
            option_type="CE",
            token=FAR_TOKEN,
            symbol="NIFTY26SEP24000CE",
            strike=24000.0,
            when=when,
            ltp=40.0,
        )
        result = _prove_side_tick(
            item_id="real_zerodha_ce_tick",
            option_type="CE",
            provider=provider,
            quotes=[wrong],
        )
        self.assertEqual(result.status, "FAIL")
        self.assertIn("did not match selected contract", result.detail)
        self.assertIn(str(provider.selected.instrument_token), result.evidence)
        self.assertIn(str(FAR_TOKEN), result.evidence)
        provider.disconnect()

    def test_different_pe_tick_fails_selected_identity(self):
        when = AS_OF - timedelta(seconds=2)
        clock = FrozenClock(AS_OF)
        provider = _connected_provider([], clock=clock)
        wrong_token = 424242
        wrong = _usable_quote(
            option_type="PE",
            token=wrong_token,
            symbol="NIFTY26SEP24500PE",
            strike=24500.0,
            when=when,
            ltp=12.0,
        )
        result = _prove_side_tick(
            item_id="real_zerodha_pe_tick",
            option_type="PE",
            provider=provider,
            quotes=[wrong],
        )
        self.assertEqual(result.status, "FAIL")
        self.assertIn("did not match selected contract", result.detail)
        self.assertIn(str(provider.selected_put.instrument_token), result.evidence)
        self.assertIn(str(wrong_token), result.evidence)
        provider.disconnect()

    def test_exact_selected_ce_tick_passes(self):
        when = AS_OF - timedelta(seconds=2)
        clock = FrozenClock(AS_OF)
        provider = _connected_provider([], clock=clock)
        selected = provider.selected
        quote = _usable_quote(
            option_type="CE",
            token=selected.instrument_token,
            symbol=selected.tradingsymbol,
            strike=selected.strike,
            when=when,
            ltp=101.5,
        )
        result = _prove_side_tick(
            item_id="real_zerodha_ce_tick",
            option_type="CE",
            provider=provider,
            quotes=[quote],
        )
        self.assertEqual(result.status, "PASS")
        self.assertIn(f"selected_contract={selected.tradingsymbol}", result.evidence)
        self.assertIn(f"received_token={selected.instrument_token}", result.evidence)
        self.assertIn("ltp=101.5", result.evidence)
        provider.disconnect()

    def test_exact_selected_pe_tick_passes(self):
        when = AS_OF - timedelta(seconds=2)
        clock = FrozenClock(AS_OF)
        provider = _connected_provider([], clock=clock)
        selected = provider.selected_put
        quote = _usable_quote(
            option_type="PE",
            token=selected.instrument_token,
            symbol=selected.tradingsymbol,
            strike=selected.strike,
            when=when,
            ltp=88.0,
        )
        result = _prove_side_tick(
            item_id="real_zerodha_pe_tick",
            option_type="PE",
            provider=provider,
            quotes=[quote],
        )
        self.assertEqual(result.status, "PASS")
        self.assertIn(f"selected_contract={selected.tradingsymbol}", result.evidence)
        self.assertIn(f"received_token={selected.instrument_token}", result.evidence)
        provider.disconnect()

    def test_reconnect_without_post_tick_fails(self):
        when = AS_OF - timedelta(seconds=2)
        clock = FrozenClock(AS_OF)
        frames = [
            _frame(_full_packet(CE_TOKEN, ltp=101.5, bid=101.0, ask=102.0, volume=40, oi=80, when=when)),
            _frame(_full_packet(PE_TOKEN, ltp=88.0, bid=87.5, ask=88.5, volume=12, oi=30, when=when)),
        ]
        hooks = LiveProofHooks(
            open_provider=_open_scripted(frames, clock=clock),
            poll_attempts=6,
            reconnect_wait_seconds=1.0,
        )
        results = run_zerodha_live_proofs(environ=CRED_ENV, hooks=hooks, clock=clock)
        by_id = {item.id: item for item in results}
        self.assertEqual(by_id["real_zerodha_ce_tick"].status, "PASS")
        self.assertEqual(by_id["real_zerodha_pe_tick"].status, "PASS")
        self.assertEqual(by_id["websocket_reconnect_live_proof"].status, "FAIL")
        self.assertIn("no NEW post-reconnect", by_id["websocket_reconnect_live_proof"].detail)

    def test_reconnect_with_new_post_tick_passes(self):
        when = AS_OF - timedelta(seconds=2)
        clock = FrozenClock(AS_OF)
        hooks = LiveProofHooks(
            open_provider=_open_scripted(_ce_pe_frames(when), clock=clock),
            poll_attempts=8,
            reconnect_wait_seconds=1.0,
        )
        results = run_zerodha_live_proofs(environ=CRED_ENV, hooks=hooks, clock=clock)
        reconnect = next(item for item in results if item.id == "websocket_reconnect_live_proof")
        self.assertEqual(reconnect.status, "PASS", reconnect.detail)
        self.assertIn("reconnect_count=", reconnect.evidence)
        self.assertIn("post_reconnect_quote=", reconnect.evidence)
        self.assertIn("ts=", reconnect.evidence)

    def test_pre_disconnect_quote_does_not_satisfy_post_reconnect(self):
        when = AS_OF - timedelta(seconds=2)
        clock = FrozenClock(AS_OF)
        # Third frame intentionally identical to the first CE tick fingerprint.
        frames = [
            _frame(_full_packet(CE_TOKEN, ltp=101.5, bid=101.0, ask=102.0, volume=40, oi=80, when=when)),
            _frame(_full_packet(PE_TOKEN, ltp=88.0, bid=87.5, ask=88.5, volume=12, oi=30, when=when)),
            _frame(_full_packet(CE_TOKEN, ltp=101.5, bid=101.0, ask=102.0, volume=40, oi=80, when=when)),
        ]
        hooks = LiveProofHooks(
            open_provider=_open_scripted(frames, clock=clock),
            poll_attempts=8,
            reconnect_wait_seconds=1.0,
        )
        results = run_zerodha_live_proofs(environ=CRED_ENV, hooks=hooks, clock=clock)
        reconnect = next(item for item in results if item.id == "websocket_reconnect_live_proof")
        self.assertEqual(reconnect.status, "FAIL", reconnect.detail)
        self.assertIn("no NEW post-reconnect", reconnect.detail)

    def test_reconnect_helper_rejects_healthy_without_new_tick(self):
        when = AS_OF - timedelta(seconds=2)
        clock = FrozenClock(AS_OF)
        provider = _connected_provider(
            [
                _frame(_full_packet(CE_TOKEN, ltp=101.5, bid=101.0, ask=102.0, volume=40, oi=80, when=when)),
            ],
            clock=clock,
        )
        # Drain the only frame as "pre" data, then reconnect with empty feed.
        pre = [
            _usable_quote(
                option_type="CE",
                token=CE_TOKEN,
                symbol=provider.selected.tradingsymbol,
                strike=provider.selected.strike,
                when=when,
                ltp=101.5,
            )
        ]
        provider.poll()  # consume the CE frame so reconnect has nothing new
        result = _prove_websocket_reconnect(
            provider,
            attempts=6,
            max_staleness_seconds=30,
            reconnect_wait_seconds=1.0,
            calendar=_calendar(clock),
            pre_reconnect_quotes=pre,
        )
        self.assertEqual(result.status, "FAIL")
        self.assertIn("no NEW post-reconnect", result.detail)
        provider.disconnect()


if __name__ == "__main__":
    unittest.main()
