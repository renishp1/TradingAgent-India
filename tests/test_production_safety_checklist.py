"""Tests for the post-Phase-13 production safety checklist."""

from __future__ import annotations

import json
import unittest

from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.safety import (
    CHECKLIST_SCHEMA,
    ProductionSafetyReport,
    run_production_safety_checklist,
)
from grow.safety.checklist import ChecklistItem


class ProductionSafetyChecklistTests(unittest.TestCase):
    def test_checklist_runs_and_reports_schema(self):
        report = run_production_safety_checklist()
        self.assertIsInstance(report, ProductionSafetyReport)
        payload = report.to_dict()
        self.assertEqual(payload["schema"], CHECKLIST_SCHEMA)
        self.assertFalse(payload["live_trading_qualification_ready"])
        self.assertTrue(payload["paper_mode"])
        self.assertFalse(payload["live_trading"])
        self.assertFalse(payload["broker_order_path"])
        self.assertEqual(report.passed + report.failed + report.blocked, len(report.items))
        # Report must serialize cleanly for the CLI script.
        json.dumps(payload)

    def test_compile_lock_and_paper_invariants_pass(self):
        report = run_production_safety_checklist()
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

    def test_live_zerodha_proofs_are_blocked(self):
        report = run_production_safety_checklist()
        blocked_ids = {
            "real_zerodha_ce_tick",
            "real_zerodha_pe_tick",
            "websocket_reconnect_live_proof",
            "real_option_chain_live_proof",
        }
        by_id = {item.id: item for item in report.items}
        for item_id in blocked_ids:
            self.assertEqual(by_id[item_id].status, "BLOCKED", item_id)
            self.assertIn("AUTH_MISSING", by_id[item_id].detail + by_id[item_id].evidence)
        self.assertEqual(report.blocked, 4)
        self.assertFalse(report.live_trading_qualification_ready)

    def test_phase12_phase13_surfaces_present(self):
        report = run_production_safety_checklist()
        by_id = {item.id: item for item in report.items}
        self.assertEqual(by_id["historical_validation_pit"].status, "PASS")
        self.assertEqual(by_id["paper_campaign_infrastructure"].status, "PASS")
        self.assertEqual(by_id["evaluation_labels"].status, "PASS")
        self.assertEqual(by_id["complete_audit_journal"].status, "PASS")
        self.assertEqual(by_id["deterministic_replay"].status, "PASS")

    def test_no_failed_items_on_current_main(self):
        report = run_production_safety_checklist()
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


if __name__ == "__main__":
    unittest.main()
