from __future__ import annotations

import ast
import json
import pathlib
import tempfile
import unittest
from datetime import date

from grow.clock import FrozenClock
from grow.errors import GrowConfigError, GrowLiveTradingDisabled
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.live_data.catalog import UNKNOWN_EXPIRY_CLASS
from grow.live_data.expiry_class import CALENDAR_UNSUPPORTED_YEAR
from grow.live_data.loop import LivePaperLoop
from grow.live_data.models import CycleStatus
from grow.live_data.smoke import (
    FAIL,
    HARD_FAIL,
    PASS,
    PASS_WITH_NO_TRADE,
    SMOKE_SCHEMA,
    build_smoke_report,
    classification_by_underlying,
    classify_smoke_result,
    contains_secret,
    drain_smoke_loop,
    load_smoke_secrets,
    redact_tree,
    scan_broker_source,
    smoke_config,
    write_smoke_report,
)
from grow.live_data.truedata import TrueDataAdapter
from tests.helpers import TEST_RISK_SECRET
from tests.test_live_data import AS_OF, _live_config
from tests.test_truedata import (
    _auth_ok,
    _nifty_catalog,
    _real_adapter,
    _settings,
    _symbolsadded,
    _td_symbol,
    _trade,
    bullish_event,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _protocol_loop():
    catalog = _nifty_catalog()
    ticks = [_trade(catalog["mapping"][0][1], float(catalog["spots"]["NIFTY"]), seq=1)]
    seq = 2
    by_symbol = {
        _td_symbol(row["underlying"], row["expiry"], row["strike"], row["option_type"]): row
        for row in bullish_event(underlyings=("NIFTY",))["contract_master"]
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
    return loop, catalog


class SmokeGateTests(unittest.TestCase):
    def test_missing_smoke_flag_and_credentials(self) -> None:
        with self.assertRaises(GrowConfigError) as ctx:
            load_smoke_secrets({})
        self.assertIn("SMOKE_DISABLED", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            load_smoke_secrets({"TRUEDATA_SMOKE": "1"})
        self.assertIn("AUTH_MISSING", str(ctx.exception))

    def test_malformed_live_trading_is_forbidden(self) -> None:
        with self.assertRaises(GrowLiveTradingDisabled):
            smoke_config(
                {
                    "TRUEDATA_SMOKE": "1",
                    "TRUEDATA_USERNAME": "user",
                    "TRUEDATA_PASSWORD": "password-value-ok",
                    "GROW_LIVE_TRADING": "true",
                }
            )

    def test_smoke_config_is_paper_real_truedata(self) -> None:
        cfg = smoke_config(
            {
                "TRUEDATA_SMOKE": "1",
                "TRUEDATA_USERNAME": "user",
                "TRUEDATA_PASSWORD": "password-value-ok",
                "GROW_RISK_SECRET": TEST_RISK_SECRET,
            }
        )
        self.assertTrue(cfg.live_data.enabled)
        self.assertEqual(cfg.live_data.provider, "truedata")
        self.assertEqual(cfg.live_data.mode, "real")
        self.assertTrue(cfg.live_data.paper_mode)
        self.assertFalse(cfg.live_data.live_trading)
        self.assertEqual(cfg.live_data.snapshot_interval_seconds, 0)
        self.assertFalse(cfg.execution.live_trading_enabled)

    def test_script_refuses_catalog_injection_and_broker(self) -> None:
        text = (ROOT / "scripts" / "run_truedata_smoke.py").read_text(encoding="utf-8")
        self.assertNotIn("catalog_loader", text)
        self.assertNotIn("sample_ticks", text)
        self.assertNotIn("place_order", text)
        self.assertNotIn("kiteconnect", text.lower())
        tree = ast.parse(text)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertTrue({"place_order", "LiveBroker", "kiteconnect"}.isdisjoint(names))


class SmokeReportTests(unittest.TestCase):
    def test_verdict_matrix(self) -> None:
        self.assertEqual(
            classify_smoke_result(
                authenticated=True,
                catalog_ok=True,
                mapping_ready=True,
                snapshot_ok=True,
                paper_fill=True,
                hard_fail=False,
                fixture_fallback=False,
            ),
            PASS,
        )
        self.assertEqual(
            classify_smoke_result(
                authenticated=True,
                catalog_ok=True,
                mapping_ready=True,
                snapshot_ok=True,
                paper_fill=False,
                hard_fail=False,
                fixture_fallback=False,
            ),
            PASS_WITH_NO_TRADE,
        )
        self.assertEqual(
            classify_smoke_result(
                authenticated=True,
                catalog_ok=True,
                mapping_ready=False,
                snapshot_ok=False,
                paper_fill=False,
                hard_fail=False,
                fixture_fallback=False,
            ),
            FAIL,
        )
        self.assertEqual(
            classify_smoke_result(
                authenticated=True,
                catalog_ok=True,
                mapping_ready=True,
                snapshot_ok=True,
                paper_fill=True,
                hard_fail=False,
                fixture_fallback=True,
            ),
            HARD_FAIL,
        )

    def test_redacts_secrets(self) -> None:
        payload = {"ok": True, "password": "super-secret-value", "note": "user super-secret-value here"}
        redacted = redact_tree(payload, ("super-secret-value",))
        self.assertEqual(redacted["password"], "[REDACTED]")
        self.assertNotIn("super-secret-value", json.dumps(redacted))
        self.assertFalse(contains_secret(redacted, ("super-secret-value",)))

    def test_protocol_path_writes_non_secret_report(self) -> None:
        loop, catalog = _protocol_loop()
        reports = drain_smoke_loop(loop)
        report = build_smoke_report(
            loop,
            reports,
            secrets=("super-secret-value", "user"),
            root=ROOT,
        )
        self.assertEqual(report["schema"], SMOKE_SCHEMA)
        self.assertTrue(report["config"]["paper_mode"])
        self.assertFalse(report["config"]["live_trading"])
        self.assertTrue(report["connection"]["authenticated"])
        self.assertGreaterEqual(report["catalog"]["count"], 1)
        self.assertTrue(report["subscription"]["mapping_ready"])
        self.assertTrue(report["subscription"]["acknowledged"])
        self.assertIsNotNone(report["first_tick"])
        self.assertEqual(report["first_tick"]["provider_id"], loop.provider.identity)
        self.assertIsNotNone(report["first_tick"]["provider_symbol_id"])
        self.assertIsNotNone(report["first_tick"]["canonical_id"])
        self.assertIn("2b_strategy", report["pipeline"]["stages"])
        self.assertTrue(report["safety"]["paper_only"])
        self.assertFalse(report["safety"]["broker"])
        self.assertFalse(report["safety"]["catalog_injected"])
        self.assertFalse(report["safety"]["fixture_fallback"])
        self.assertIn(report["result"], {PASS, PASS_WITH_NO_TRADE})
        self.assertTrue(any(row.status is CycleStatus.PAPER_FILL for row in reports) or report["result"] == PASS_WITH_NO_TRADE)
        self.assertFalse(contains_secret(report, ("super-secret-value",)))
        self.assertFalse(LIVE_TRADING_COMPILED)
        self.assertEqual(scan_broker_source(ROOT), ())
        with tempfile.TemporaryDirectory() as tmp:
            path = write_smoke_report(report, pathlib.Path(tmp) / "smoke.json")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["schema"], SMOKE_SCHEMA)
            self.assertNotIn("super-secret-value", path.read_text(encoding="utf-8"))
        loop.stop()
        self.assertGreaterEqual(len(catalog["instruments"]), 1)

    def test_fixture_fallback_is_hard_fail(self) -> None:
        loop, _catalog = _protocol_loop()
        reports = drain_smoke_loop(loop)
        report = build_smoke_report(loop, reports, root=ROOT, error="FIXTURE_FALLBACK_FORBIDDEN")
        self.assertEqual(report["result"], HARD_FAIL)
        self.assertTrue(report["safety"]["fixture_fallback"])
        loop.stop()

    def test_auth_failed_is_fail_not_pass(self) -> None:
        catalog = _nifty_catalog()
        adapter, _sock = _real_adapter(['{"success": false, "message": "invalid login"}'], catalog)
        cfg = _live_config(provider="truedata", snapshot_interval_seconds=0, session_timeout_seconds=86400)
        loop = LivePaperLoop(cfg, adapter, clock=FrozenClock(AS_OF), risk_secret=TEST_RISK_SECRET)
        with self.assertRaises(GrowConfigError) as ctx:
            loop.start()
        self.assertIn("AUTH_FAILED", str(ctx.exception))
        report = build_smoke_report(loop, list(loop.cycles), root=ROOT, error=str(ctx.exception))
        self.assertEqual(report["result"], FAIL)
        self.assertFalse(report["connection"]["authenticated"])
        self.assertIsNone(report["first_tick"])
        self.assertFalse(report["subscription"]["mapping_ready"])

    def test_live_execution_error_is_hard_fail(self) -> None:
        loop, _catalog = _protocol_loop()
        reports = drain_smoke_loop(loop)
        report = build_smoke_report(loop, reports, root=ROOT, error="LIVE_EXECUTION_FORBIDDEN")
        self.assertEqual(report["result"], HARD_FAIL)
        loop.stop()


class SmokeClassificationTests(unittest.TestCase):
    def test_2026_classified_and_2027_excluded_from_summary(self) -> None:
        adapter = TrueDataAdapter(
            events=(),
            catalog=[
                {"provider_symbol": "NIFTY 50", "lot_size": None},
                {"provider_symbol": "NIFTY26092225000CE", "lot_size": 75, "expiry": date(2026, 9, 22), "option_type": "CE"},
                {"provider_symbol": "NIFTY27092125000CE", "lot_size": 75, "expiry": date(2027, 9, 21), "option_type": "CE"},
            ],
            settings=_settings(),
            clock=FrozenClock(AS_OF),
        )
        adapter._spots["NIFTY"] = 25000.0
        adapter.connect()
        summary = classification_by_underlying(adapter.instrument_catalog())
        self.assertGreaterEqual(summary["NIFTY"]["WEEKLY"], 1)
        self.assertGreaterEqual(summary["NIFTY"]["UNKNOWN"], 1)
        self.assertGreaterEqual(summary["NIFTY"]["UNSUPPORTED_YEAR"], 1)
        by_symbol = {row["provider_symbol"]: row for row in adapter.instrument_catalog()}
        self.assertEqual(by_symbol["NIFTY27092125000CE"]["expiry_class"], UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(by_symbol["NIFTY27092125000CE"]["classification"]["diagnostic"], CALENDAR_UNSUPPORTED_YEAR)
        self.assertTrue(any("NIFTY26092225000CE" in symbol for symbol in adapter._desired))
        self.assertFalse(any("NIFTY27092125000CE" in symbol for symbol in adapter._desired))


class SmokeScriptProcessTests(unittest.TestCase):
    def test_main_without_flag_is_disabled(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "run_truedata_smoke",
            ROOT / "scripts" / "run_truedata_smoke.py",
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        import io
        from contextlib import redirect_stderr

        buf = io.StringIO()
        with redirect_stderr(buf):
            code = mod.main({"PATH": "/usr/bin"})
        self.assertEqual(code, 2)
        self.assertIn("SMOKE_DISABLED", buf.getvalue())
