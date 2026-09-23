"""Read-only dashboard API tests — capital/risk/market accuracy + safety."""

from __future__ import annotations

import ast
import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from grow.campaign.config import campaign_paper_config
from grow.config import load_config
from grow.dashboard.app import create_app
from grow.dashboard.service import DashboardService, align_dashboard_config, redact_secrets
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.paper.ledger import PaperBook, Position
from grow.types import Symbol

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "grow.default.yaml"

_TEST_ENV = {
    "GROW_EXECUTION_MODE": "paper",
    "GROW_LIVE_TRADING": "false",
    "LIVE_TRADING_ENABLED": "false",
    "GROW_RISK_SECRET": "unit-test-dashboard-secret-32chars",
}


def _service(**kwargs) -> DashboardService:
    config = load_config(CONFIG, environ=dict(_TEST_ENV))
    return DashboardService(config, **kwargs)


def _client(service: DashboardService | None = None) -> TestClient:
    return TestClient(create_app(service or _service()))


class DashboardApiTests(unittest.TestCase):
    def test_health(self) -> None:
        res = _client().get("/api/health")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["read_only"])
        self.assertFalse(body["live_trading"])
        self.assertFalse(body["broker_order_path"])

    def test_dashboard_snapshot(self) -> None:
        res = _client().get("/api/dashboard")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("safety", body)
        self.assertIn("system", body)
        self.assertIn("capital", body)
        self.assertIn("snapshot", body)
        self.assertEqual(body["safety"]["banners"][0], "PAPER TRADING")
        self.assertFalse(body["system"]["live_trading_compiled"])
        self.assertEqual(body["system"]["broker_order_path"], "DISABLED")
        self.assertIn(
            body["market"]["market_data"],
            {
                "NOT CONNECTED",
                "TOKEN PRESENT (not verified)",
                "CONNECTED",
            },
        )
        self.assertEqual(body["system"]["market_data_status"], body["market"]["market_data"])

    def test_config_endpoint(self) -> None:
        res = _client().get("/api/config")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["execution"]["mode"], "paper")
        self.assertFalse(body["execution"]["live_trading_enabled"])
        self.assertIn("starting_cash", body["paper"])
        self.assertIn("max_daily_loss", body["risk"])

    def test_positions_empty(self) -> None:
        res = _client().get("/api/positions")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["open_count"], 0)
        self.assertEqual(body["message"], "No open paper positions")

    def test_positions_from_book(self) -> None:
        config = load_config(CONFIG, environ=dict(_TEST_ENV))
        book = PaperBook(cash=9_000, currency="INR")
        book.positions["RELIANCE"] = Position(
            symbol=Symbol("RELIANCE"),
            quantity=10,
            average_price=2500.0,
        )
        svc = DashboardService(config, book=book, checkpoint_path=Path("__none__"))
        svc.checkpoint_path = None
        res = _client(svc).get("/api/positions")
        body = res.json()
        self.assertEqual(body["open_count"], 1)
        self.assertEqual(body["positions"][0]["symbol"], "RELIANCE")
        self.assertEqual(body["positions"][0]["current_price"], "NOT AVAILABLE")

    def test_risk_endpoint(self) -> None:
        res = _client().get("/api/risk")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "ACTIVE")
        self.assertTrue(body["read_only"])
        effective = align_dashboard_config(load_config(CONFIG, environ=dict(_TEST_ENV)))
        self.assertEqual(body["max_daily_loss"], effective.risk.max_daily_loss)
        self.assertEqual(body["paper_capital"], effective.paper.starting_cash)
        self.assertEqual(body["starting_capital"], effective.paper.starting_cash)

    def test_dashboard_capital_equals_effective_growconfig(self) -> None:
        """Dashboard capital must match campaign/RiskGuard effective profile.

        ``GROW_STARTING_CASH`` can overlay YAML to ₹10L while the named paper
        profile (INDIA_INDEX_OPTIONS_PAPER_10K) used by RiskGuard remains ₹10K.
        """
        env = dict(_TEST_ENV)
        env["GROW_STARTING_CASH"] = "1000000"
        raw = load_config(CONFIG, environ=env)
        self.assertEqual(raw.paper.starting_cash, 1_000_000.0)
        effective = campaign_paper_config(raw)
        self.assertEqual(effective.paper.starting_cash, 10_000.0)
        self.assertEqual(effective.risk.max_daily_loss, 2_000.0)
        self.assertEqual(effective.risk.max_per_trade_risk, 1_000.0)
        self.assertEqual(effective.risk.max_open_positions, 2)

        svc = DashboardService(raw, checkpoint_path=Path("__none__"))
        svc.checkpoint_path = None
        dash = svc.dashboard()
        self.assertEqual(dash["capital"]["paper_capital"], effective.paper.starting_cash)
        self.assertEqual(dash["capital"]["available_capital"], effective.paper.starting_cash)
        self.assertEqual(dash["risk"]["paper_capital"], effective.paper.starting_cash)
        self.assertEqual(dash["risk"]["max_daily_loss"], 2_000.0)
        self.assertEqual(dash["risk"]["max_per_trade_risk"], 1_000.0)
        self.assertEqual(dash["risk"]["max_open_positions"], 2)
        self.assertEqual(dash["capital"]["capital_profile"], "INDIA_INDEX_OPTIONS_PAPER_10K")

        from grow.dashboard.service import build_service_from_environ

        built = build_service_from_environ(env, config_path=CONFIG, checkpoint_path=Path("__none__"))
        built.checkpoint_path = None
        self.assertEqual(built.config.paper.starting_cash, 10_000.0)
        self.assertEqual(built.dashboard()["capital"]["paper_capital"], 10_000.0)

    def test_agents_unavailable_without_cycle(self) -> None:
        res = _client().get("/api/agents")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertFalse(body["available"])
        self.assertEqual(body["message"], "AI decision data not available")

    def test_stale_market_data_shows_no_trade(self) -> None:
        snap = {
            "snapshot_id": "snap-stale-1",
            "session_date": "2026-09-23",
            "market_data_source": "LIVE",
            "data_quality": "STALE",
            "quality_notes": ["DATA_STALE", "quote_age_high"],
            "underlyings": {
                "NIFTY": {
                    "ltp": 25000.0,
                    "quote_timestamp": "2026-09-23T10:00:00+05:30",
                    "quote_age_seconds": 3600,
                }
            },
            "option_contracts": [],
        }
        svc = _service(market_snapshot=snap, checkpoint_path=Path("__none__"))
        svc.checkpoint_path = None
        agents = svc.agents_view()
        self.assertTrue(agents["available"])
        self.assertEqual(agents["decisions"][0]["final_action"], "NO TRADE")
        self.assertIn("STALE", agents["decisions"][0]["reason"])
        market = svc.market_status()
        self.assertEqual(market["indices"]["NIFTY"]["price"], 25000.0)
        self.assertEqual(market["freshness"], "STALE")
        self.assertEqual(market["provenance"], "LIVE")
        self.assertIn("fail-closed", market["note"].lower())

    def test_provenance_live_fixture_mixed(self) -> None:
        for source, expect_no_trade in (("LIVE", False), ("FIXTURE", False), ("MIXED", True)):
            snap = {
                "snapshot_id": f"snap-{source}",
                "session_date": "2026-09-23",
                "market_data_source": source,
                "data_quality": "OK",
                "quality_notes": [],
                "underlyings": {
                    "NIFTY": {"ltp": 100.0, "quote_timestamp": "2026-09-23T10:00:00+05:30"}
                },
                "option_contracts": [],
            }
            svc = _service(market_snapshot=snap, checkpoint_path=Path("__none__"))
            svc.checkpoint_path = None
            market = svc.market_status()
            self.assertEqual(market["provenance"], source)
            if expect_no_trade:
                agents = svc.agents_view()
                self.assertEqual(agents["decisions"][0]["final_action"], "NO TRADE")
                self.assertIn("MIXED", agents["decisions"][0]["reason"])

    def test_option_chain_from_snapshot(self) -> None:
        snap = {
            "snapshot_id": "snap-chain",
            "session_date": "2026-09-23",
            "market_data_source": "FIXTURE",
            "data_quality": "OK",
            "quality_notes": [],
            "underlyings": {},
            "option_contracts": [
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-09-30",
                    "strike": 25000.0,
                    "option_type": "CE",
                    "ltp": 120.0,
                    "bid": 118.0,
                    "ask": 122.0,
                    "provider_contract_id": "NIFTY25000CE",
                    "quality": "OK",
                },
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-09-30",
                    "strike": 25000.0,
                    "option_type": "PE",
                    "ltp": 95.0,
                    "bid": 93.0,
                    "ask": 97.0,
                    "provider_contract_id": "NIFTY25000PE",
                    "quality": "OK",
                },
            ],
        }
        decision = {
            "analysis_cycle_id": "cycle-1",
            "snapshot_id": "snap-chain",
            "status": "NO_TRADE",
            "action": "NO_TRADE",
            "reason_codes": ["CHAIN_FILTER_REJECTED"],
            "risk_guard_result": "NOT_EVALUATED",
            "risk_guard_reason": "NOT_EVALUATED",
            "trade_candidate": None,
            "campaign_signal": {
                "instrument": "NIFTY25000CE",
                "score": 0.42,
                "provider_contract_id": "NIFTY25000CE",
                "dte_days": 7,
            },
            "gate_results": [],
        }
        svc = _service(
            market_snapshot=snap,
            last_cycle={"cycle_id": "cycle-1", "snapshot_id": "snap-chain", "decision": decision},
            checkpoint_path=Path("__none__"),
        )
        svc.checkpoint_path = None
        chain = svc.option_chain_view()
        self.assertTrue(chain["available"])
        self.assertEqual(len(chain["rows"]), 1)
        row = chain["rows"][0]
        self.assertEqual(row["strike"], 25000.0)
        self.assertEqual(row["ce_ltp"], 120.0)
        self.assertEqual(row["pe_ask"], 97.0)
        self.assertEqual(row["dte"], 7)
        self.assertEqual(row["score"], 0.42)
        self.assertEqual(row["rejection_reason"], "CHAIN_FILTER_REJECTED")

        agents = svc.agents_view()
        self.assertEqual(agents["decisions"][0]["final_action"], "NO TRADE")
        self.assertEqual(agents["decisions"][0]["cycle_id"], "cycle-1")
        self.assertIn("CHAIN_FILTER_REJECTED", agents["decisions"][0]["reason"])

    def test_decision_candidate_fields(self) -> None:
        decision = {
            "analysis_cycle_id": "cycle-buy",
            "snapshot_id": "snap-buy",
            "status": "CANDIDATE",
            "action": "BUY_CE",
            "reason_codes": ["CAMPAIGN_SELECTED"],
            "risk_guard_result": "APPROVED",
            "risk_guard_reason": "ok",
            "trade_candidate": {
                "instrument": "NIFTY-25000-CE",
                "underlying": "NIFTY",
                "option_type": "CE",
                "strike": 25000.0,
                "expiry": "2026-09-30",
                "confidence": 0.7,
            },
            "campaign_signal": {"score": 0.9, "dte_days": 5, "option_type": "CE"},
            "gate_results": [{"gate": "ceo_gate", "passed": True, "detail": "ok"}],
            "decision_timestamp": "2026-09-23T10:15:00+05:30",
        }
        svc = _service(
            last_cycle={"cycle_id": "cycle-buy", "decision": decision},
            market_snapshot={
                "snapshot_id": "snap-buy",
                "market_data_source": "LIVE",
                "data_quality": "OK",
            },
            checkpoint_path=Path("__none__"),
        )
        svc.checkpoint_path = None
        row = svc.agents_view()["decisions"][0]
        self.assertEqual(row["final_action"], "BUY_CE")
        self.assertEqual(row["ceo_gate"], "PASS")
        self.assertEqual(row["risk_guard"], "APPROVED")
        self.assertEqual(row["strike"], 25000.0)
        self.assertEqual(row["score"], 0.9)
        self.assertEqual(row["provenance"], "LIVE")

    def test_safety_status(self) -> None:
        res = _client().get("/api/safety")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["paper_trading"])
        self.assertTrue(body["live_trading_disabled"])
        self.assertTrue(body["broker_orders_disabled"])
        self.assertFalse(body["can_enable_live_trading"])
        self.assertFalse(LIVE_TRADING_COMPILED)
        self.assertFalse(body["live_trading_compiled"])
        self.assertEqual(
            body["banners"],
            [
                "PAPER TRADING",
                "LIVE TRADING DISABLED",
                "BROKER ORDERS DISABLED",
                "READ ONLY UI",
            ],
        )

    def test_api_does_not_expose_secrets(self) -> None:
        svc = _service()
        dirty = {
            "GROW_RISK_SECRET": "should-not-leak",
            "KITE_API_KEY": "kite-key",
            "KITE_ACCESS_TOKEN": "kite-token",
            "OPENAI_API_KEY": "sk-test",
            "nested": {"api_key": "x", "ok": 1},
            "safe": "visible",
        }
        clean = redact_secrets(dirty)
        blob = str(clean).lower()
        self.assertNotIn("should-not-leak", blob)
        self.assertNotIn("kite-key", blob)
        self.assertNotIn("kite-token", blob)
        self.assertNotIn("sk-test", blob)
        self.assertEqual(clean["safe"], "visible")
        self.assertEqual(clean["nested"]["ok"], 1)
        self.assertNotIn("api_key", clean["nested"])

        for path in ("/api/dashboard", "/api/config", "/api/risk", "/api/settings"):
            text = _client(svc).get(path).text.lower()
            for needle in (
                "grow_risk_secret",
                "kite_api_key",
                "kite_access_token",
                "openai_api_key",
                "should-not-leak",
            ):
                self.assertNotIn(needle, text)

    def test_api_cannot_enable_live_trading(self) -> None:
        client = _client()
        for path in (
            "/api/enable-live",
            "/api/live",
            "/api/orders",
            "/api/buy",
            "/api/sell",
            "/api/config",
        ):
            res = client.post(path, json={"live_trading_enabled": True})
            self.assertEqual(res.status_code, 405)
            body = res.json()
            self.assertFalse(body.get("live_trading", False))
            self.assertFalse(body.get("can_enable_live_trading", True))

    def test_ui_index_served(self) -> None:
        res = _client().get("/")
        self.assertEqual(res.status_code, 200)
        text = res.text
        self.assertIn("PAPER TRADING", text)
        self.assertIn("LIVE TRADING DISABLED", text)
        self.assertIn("BROKER ORDERS DISABLED", text)
        self.assertNotIn("Enable Live Trading", text)
        self.assertNotIn(">Buy<", text)
        self.assertNotIn(">Sell<", text)
        js = _client().get("/static/app.js").text
        self.assertNotIn("Dashboard Phase 1 does not connect to live market data.", js)
        self.assertNotIn("Option chain not connected in dashboard Phase 1", js)

    def test_dashboard_does_not_call_broker_order_apis(self) -> None:
        """Static scan: dashboard modules never import broker SDKs or place orders."""
        forbidden_imports = {
            "kiteconnect",
            "upstox",
            "dhanhq",
            "broker",
        }
        forbidden_attrs = {
            "place_order",
            "placeOrder",
            "LiveBroker",
        }
        dash_dir = ROOT / "grow" / "dashboard"
        for path in dash_dir.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        self.assertNotIn(root, forbidden_imports, path.name)
                if isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    self.assertNotIn(root, forbidden_imports, path.name)
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, forbidden_attrs, path.name)
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, forbidden_attrs, path.name)

        src = inspect.getsource(DashboardService)
        self.assertNotIn("PaperExecutionEngine", src)
        self.assertNotIn("RiskGuard(", src)

    def test_boot_strips_broker_credentials(self) -> None:
        """Dashboard starts without Zerodha creds even if present in environ."""
        from grow.dashboard.service import build_service_from_environ

        env = dict(_TEST_ENV)
        env["KITE_API_KEY"] = "must-not-block-dashboard"
        env["KITE_ACCESS_TOKEN"] = "must-not-block-dashboard"
        svc = build_service_from_environ(env, config_path=CONFIG)
        self.assertEqual(svc.config.execution.mode, "paper")
        client = _client(svc)
        self.assertEqual(client.get("/api/health").status_code, 200)
        text = client.get("/api/dashboard").text
        self.assertNotIn("must-not-block-dashboard", text)

    def test_live_trading_compiled_remains_false(self) -> None:
        self.assertIs(LIVE_TRADING_COMPILED, False)
        body = _client().get("/api/safety").json()
        self.assertFalse(body["live_trading_compiled"])

    def test_replay_artifact_hydration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = {
                "schema": "campaign.trade_replay.v1",
                "snapshot_id": "snap-r",
                "cycle_id": "cycle-r",
                "decision_id": "dec-r",
                "decision": {
                    "analysis_cycle_id": "cycle-r",
                    "snapshot_id": "snap-r",
                    "status": "NO_TRADE",
                    "action": "NO_TRADE",
                    "reason_codes": ["NO_VALID_STRATEGY_CANDIDATE"],
                    "risk_guard_result": "NOT_EVALUATED",
                    "risk_guard_reason": "NOT_EVALUATED",
                    "trade_candidate": None,
                    "campaign_signal": None,
                    "gate_results": [],
                },
                "snapshot": {
                    "snapshot_id": "snap-r",
                    "market_data_source": "FIXTURE",
                    "data_quality": "OK",
                    "session_date": "2026-09-23",
                    "underlyings": {"NIFTY": {"ltp": 1.0}},
                    "option_contracts": [],
                },
                "market_data_provider": "FIXTURE",
            }
            (root / "dec-r.json").write_text(json.dumps(record), encoding="utf-8")
            prev = os.environ.get("GROW_DASHBOARD_REPLAY")
            os.environ["GROW_DASHBOARD_REPLAY"] = str(root)
            try:
                svc = _service(checkpoint_path=Path("__none__"))
                svc.checkpoint_path = None
                agents = svc.agents_view()
                self.assertTrue(agents["available"])
                self.assertEqual(agents["decisions"][0]["final_action"], "NO TRADE")
                self.assertEqual(svc.market_status()["provenance"], "FIXTURE")
            finally:
                if prev is None:
                    os.environ.pop("GROW_DASHBOARD_REPLAY", None)
                else:
                    os.environ["GROW_DASHBOARD_REPLAY"] = prev


if __name__ == "__main__":
    unittest.main()
