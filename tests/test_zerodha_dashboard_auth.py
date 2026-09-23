"""Zerodha Connect (market-data OAuth) for the paper dashboard."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from grow.config import load_config
from grow.dashboard.app import create_app
from grow.dashboard.service import DashboardService
from grow.dashboard import zerodha_auth as za
from grow.errors import GrowConfigError

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "grow.default.yaml"

_TEST_ENV = {
    "GROW_EXECUTION_MODE": "paper",
    "GROW_LIVE_TRADING": "false",
    "LIVE_TRADING_ENABLED": "false",
    "GROW_RISK_SECRET": "unit-test-dashboard-secret-32chars",
}


def _service() -> DashboardService:
    return DashboardService(load_config(CONFIG, environ=dict(_TEST_ENV)))


def _client(service: DashboardService | None = None) -> TestClient:
    return TestClient(create_app(service or _service()))


class ZerodhaAuthUnitTests(unittest.TestCase):
    def test_login_url_contains_api_key_param_not_secret(self) -> None:
        url = za.login_url(api_key="demoapikey123456")
        self.assertIn("kite.zerodha.com/connect/login", url)
        self.assertIn("api_key=demoapikey123456", url)
        self.assertNotIn("secret", url.lower())

    def test_upsert_dotenv_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("GROW_EXECUTION_MODE=paper\n", encoding="utf-8")
            za.upsert_dotenv_value(path, "KITE_ACCESS_TOKEN", "tokA")
            text = path.read_text(encoding="utf-8")
            self.assertIn("KITE_ACCESS_TOKEN=tokA", text)
            za.upsert_dotenv_value(path, "KITE_ACCESS_TOKEN", "tokB")
            text2 = path.read_text(encoding="utf-8")
            self.assertIn("KITE_ACCESS_TOKEN=tokB", text2)
            self.assertNotIn("tokA", text2)

    def test_complete_callback_persists_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("", encoding="utf-8")
            env = {
                "KITE_API_KEY": "k" * 16,
                "KITE_API_SECRET": "s" * 32,
            }

            def fake_exchange(request_token, *, environ=None):
                self.assertEqual(request_token, "reqtokenvalue123")
                return "access" + ("x" * 26)

            result = za.complete_callback(
                request_token="reqtokenvalue123",
                status="success",
                environ=env,
                dotenv_path=path,
                exchanger=fake_exchange,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "CONNECTED")
            self.assertIn("KITE_ACCESS_TOKEN=", path.read_text(encoding="utf-8"))
            self.assertTrue(env["KITE_ACCESS_TOKEN"].startswith("access"))
            # Public result must not echo secrets
            blob = str(result).lower()
            self.assertNotIn("reqtoken", blob)
            self.assertNotIn(env["KITE_ACCESS_TOKEN"].lower(), blob)

    def test_auth_status_not_configured(self) -> None:
        st = za.auth_status({"GROW_EXECUTION_MODE": "paper"}, probe=False)
        self.assertEqual(st.status, "NOT_CONFIGURED")
        self.assertFalse(st.can_connect)
        pub = st.to_public_dict()
        self.assertNotIn("KITE_API_KEY", str(pub))
        self.assertIn("api_key_present", pub)


class ZerodhaDashboardRouteTests(unittest.TestCase):
    def test_status_endpoint_public(self) -> None:
        res = _client().get("/api/zerodha/status")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["provider"], "zerodha")
        self.assertFalse(body["live_trading"])
        self.assertFalse(body["broker_order_path"])
        text = res.text.lower()
        self.assertNotIn("kite_api_key", text)
        self.assertNotIn("access_token=", text)

    def test_settings_includes_zerodha_block(self) -> None:
        body = _client().get("/api/settings").json()
        self.assertIn("zerodha_market_data", body)
        self.assertFalse(body["can_enable_live_trading"])
        self.assertFalse(body["can_place_orders"])

    def test_connect_requires_credentials(self) -> None:
        cleared = {
            "KITE_API_KEY": "",
            "KITE_API_SECRET": "",
            "KITE_ACCESS_TOKEN": "",
            **_TEST_ENV,
        }
        with patch.dict("os.environ", cleared, clear=False):
            # Ensure blanks win over a developer .env loaded earlier in the suite.
            for key in ("KITE_API_KEY", "KITE_API_SECRET", "KITE_ACCESS_TOKEN"):
                os.environ.pop(key, None)
            res = _client().get("/api/zerodha/connect", follow_redirects=False)
        self.assertEqual(res.status_code, 400)
        self.assertIn("AUTH_MISSING", res.json()["error"])

    def test_connect_redirects_when_configured(self) -> None:
        with patch.dict(
            "os.environ",
            {
                **_TEST_ENV,
                "KITE_API_KEY": "k" * 16,
                "KITE_API_SECRET": "s" * 32,
            },
            clear=False,
        ):
            res = _client().get("/api/zerodha/connect", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        loc = res.headers.get("location") or ""
        self.assertIn("kite.zerodha.com/connect/login", loc)
        self.assertIn("api_key=", loc)
        self.assertNotIn("secret", loc.lower())

    def test_callback_success_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("KITE_API_KEY=kkkkkkkkkkkkkkkk\nKITE_API_SECRET=ssssssssssssssssssssssssssssssss\n", encoding="utf-8")
            with patch.object(za, "resolve_dotenv_path", return_value=path), patch.object(
                za, "exchange_request_token", return_value="a" * 32
            ), patch.dict(
                "os.environ",
                {
                    **_TEST_ENV,
                    "KITE_API_KEY": "k" * 16,
                    "KITE_API_SECRET": "s" * 32,
                },
                clear=False,
            ):
                res = _client().get(
                    "/auth/zerodha/callback",
                    params={"status": "success", "request_token": "reqtokenvalue123456"},
                )
            self.assertEqual(res.status_code, 200)
            self.assertIn("Connected", res.text)
            self.assertIn("Market-data only", res.text)
            self.assertNotIn("reqtokenvalue", res.text)
            self.assertIn("KITE_ACCESS_TOKEN=", path.read_text(encoding="utf-8"))

    def test_callback_failure_status(self) -> None:
        res = _client().get(
            "/auth/zerodha/callback",
            params={"status": "failure", "request_token": "x"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Connection failed", res.text)

    def test_disconnect_clears_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("KITE_ACCESS_TOKEN=oldtokenvalue123456789012\n", encoding="utf-8")
            with patch.object(za, "resolve_dotenv_path", return_value=path), patch.dict(
                "os.environ",
                {**_TEST_ENV, "KITE_ACCESS_TOKEN": "oldtokenvalue123456789012"},
                clear=False,
            ):
                res = _client().get("/api/zerodha/disconnect")
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("oldtokenvalue", text)

    def test_ui_exposes_connect_controls(self) -> None:
        text = _client().get("/").text
        self.assertIn("Zerodha market data", text)
        self.assertIn("zerodha-actions", text)
        self.assertIn("zerodha-cred-form", text)
        self.assertIn("remembered in this browser", text.lower())
        self.assertNotIn("Save on this PC", text)
        self.assertNotIn("Enable Live Trading", text)

    def test_save_credentials_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("GROW_EXECUTION_MODE=paper\n", encoding="utf-8")
            with patch.object(za, "resolve_dotenv_path", return_value=path):
                res = _client().post(
                    "/api/zerodha/credentials",
                    json={"api_key": "k" * 16, "api_secret": "s" * 32},
                )
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["storage"], "local_dotenv")
            self.assertTrue(body["api_key_present"])
            self.assertTrue(body["api_secret_present"])
            self.assertTrue(body["can_connect"])
            text = path.read_text(encoding="utf-8")
            self.assertIn("KITE_API_KEY=", text)
            self.assertIn("KITE_API_SECRET=", text)
            # Response must not echo secrets
            blob = res.text.lower()
            self.assertNotIn("k" * 16, blob)
            self.assertNotIn("s" * 32, blob)

    def test_credentials_rejects_live_trading_flags(self) -> None:
        res = _client().post(
            "/api/zerodha/credentials",
            json={"api_key": "k" * 16, "live_trading": True},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], "REFUSED")

    def test_mutations_still_forbidden(self) -> None:
        res = _client().post("/api/zerodha/connect", json={})
        self.assertEqual(res.status_code, 405)

    def test_no_kiteconnect_import(self) -> None:
        source = (ROOT / "grow" / "dashboard" / "zerodha_auth.py").read_text(encoding="utf-8")
        self.assertNotIn("kiteconnect", source.lower())
        self.assertNotIn("place_order", source)


if __name__ == "__main__":
    unittest.main()
