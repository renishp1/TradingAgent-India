"""Production safety checklist + Zerodha live-proof outcome tests."""

from __future__ import annotations

import json
import struct
import unittest
from datetime import timedelta
from unittest.mock import patch

from grow.clock import FrozenClock
from grow.errors import GrowConfigError
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.live_data.kite_market import KiteMarketProvider, ScriptedKiteTransport
from grow.safety import (
    CHECKLIST_SCHEMA,
    LIVE_PROOF_IDS,
    LiveProofHooks,
    ProductionSafetyReport,
    run_production_safety_checklist,
    run_zerodha_live_proofs,
)
from grow.safety.checklist import ChecklistItem
from grow.safety.live_proofs import assert_live_proofs_forbid_broker_orders
from tests.test_live_data import AS_OF
from tests.test_zerodha_market import CE_TOKEN, CSV, INDEX_TOKEN, PE_TOKEN, _frame, _full_packet


def _ce_pe_frames(when):
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
        self.assertIn("option_type=CE", by_id["real_zerodha_ce_tick"].evidence)
        self.assertIn("option_type=PE", by_id["real_zerodha_pe_tick"].evidence)
        self.assertIn("reconnect_count=", by_id["websocket_reconnect_live_proof"].evidence)
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


if __name__ == "__main__":
    unittest.main()
