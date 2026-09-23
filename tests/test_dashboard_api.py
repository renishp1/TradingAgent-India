"""Phase 1 read-only dashboard API tests."""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from grow.config import load_config
from grow.dashboard.app import create_app
from grow.dashboard.service import DashboardService, redact_secrets
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
        self.assertEqual(body["market"]["market_data"], "NOT CONNECTED")

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
        book = PaperBook(cash=900_000, currency="INR")
        book.positions["RELIANCE"] = Position(
            symbol=Symbol("RELIANCE"),
            quantity=10,
            average_price=2500.0,
        )
        svc = DashboardService(config, book=book, checkpoint_path=Path("__none__"))
        # Force no checkpoint load by using missing path already set via book provided
        svc.checkpoint_path = None
        res = _client(svc).get("/api/positions")
        body = res.json()
        self.assertEqual(body["open_count"], 1)
        self.assertEqual(body["positions"][0]["symbol"], "RELIANCE")
        self.assertEqual(body["positions"][0]["current_price"], "Not available")

    def test_risk_endpoint(self) -> None:
        res = _client().get("/api/risk")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "ACTIVE")
        self.assertTrue(body["read_only"])
        config = load_config(CONFIG, environ=dict(_TEST_ENV))
        self.assertEqual(body["max_daily_loss"], config.risk.max_daily_loss)
        self.assertEqual(body["paper_capital"], config.paper.starting_cash)

    def test_agents_unavailable_without_cycle(self) -> None:
        res = _client().get("/api/agents")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertFalse(body["available"])
        self.assertEqual(body["message"], "AI decision data not available")

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
        # Inject secret-looking keys into a synthetic payload path via redact.
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

        # Service must not construct PaperExecutionEngine (would mint stamps / execute).
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


if __name__ == "__main__":
    unittest.main()
