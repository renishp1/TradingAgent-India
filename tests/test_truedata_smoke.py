from __future__ import annotations

import ast
import json
import pathlib
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta

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
    assess_option_ticks,
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
                option_tick_ok=True,
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
                option_tick_ok=True,
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
                option_tick_ok=False,
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
                option_tick_ok=True,
                paper_fill=True,
                hard_fail=False,
                fixture_fallback=True,
            ),
            HARD_FAIL,
        )
        self.assertEqual(
            classify_smoke_result(
                authenticated=True,
                catalog_ok=True,
                mapping_ready=True,
                option_tick_ok=False,
                paper_fill=False,
                hard_fail=False,
                fixture_fallback=False,
            ),
            FAIL,
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
        self.assertTrue(report["option_tick_ok"])
        self.assertIsNotNone(report["first_option_tick"])
        self.assertIn(report["first_option_tick"]["option_type"], {"CE", "PE"})
        self.assertEqual(
            report["first_option_tick"]["provider_symbol_id"],
            report["first_tick"]["provider_symbol_id"],
        )
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
        self.assertIsNone(report["first_option_tick"])
        self.assertFalse(report["option_tick_ok"])
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


def _start_smoke(incoming, catalog):
    adapter, _sock = _real_adapter(incoming, catalog)
    cfg = _live_config(provider="truedata", snapshot_interval_seconds=0, session_timeout_seconds=86400)
    loop = LivePaperLoop(cfg, adapter, clock=FrozenClock(AS_OF), risk_secret=TEST_RISK_SECRET)
    loop.start()
    return loop


def _first_ce(catalog):
    quotes_by = {
        (q["underlying"], q["expiry"], float(q["strike"]), q["option_type"]): q for q in catalog["quotes"]
    }
    instruments = {row["provider_symbol"]: row for row in catalog["instruments"]}
    for name, ident in catalog["mapping"]:
        row = instruments.get(name)
        if row is None or row.get("option_type") != "CE":
            continue
        expiry = row["expiry"]
        expiry_s = expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)
        quote = quotes_by.get(("NIFTY", expiry_s, float(row["strike"]), "CE"))
        if quote is None:
            continue
        return name, ident, quote
    raise AssertionError("nifty catalog has no mapped CE quote")


def _index_then_ce_incoming(catalog):
    _index_name, index_id = catalog["mapping"][0]
    ce_name, ce_id, quote = _first_ce(catalog)
    incoming = [
        _auth_ok(),
        _symbolsadded(catalog["mapping"]),
        _trade(index_id, float(catalog["spots"]["NIFTY"]), seq=1),
        _trade(
            ce_id,
            float(quote["ltp"]),
            bid=quote["bid"],
            ask=quote["ask"],
            seq=2,
            oi=quote["oi"],
            volume=quote["volume"],
        ),
    ]
    return incoming, (ce_name, ce_id, quote)


def _assess(loop, snapshot=None):
    snap = loop.last_snapshot if snapshot is None else snapshot
    return assess_option_ticks(
        snap,
        loop.provider._symbol_ids,
        max_staleness_seconds=loop.config.live_data.max_staleness_seconds,
        now=loop.clock.now(),
    )


def _retimed_ce(snapshot, quote_time):
    chain = snapshot.chains["NIFTY"]
    updated = []
    found = False
    for contract in chain.contracts:
        kind = str(getattr(contract.option_type, "value", contract.option_type))
        priced = contract.bid is not None or contract.ask is not None or contract.last_price is not None
        if kind == "CE" and priced and not found:
            updated.append(replace(contract, timestamp=quote_time))
            found = True
        else:
            updated.append(contract)
    if not found:
        raise AssertionError("priced CE quote missing")
    chains = dict(snapshot.chains)
    chains["NIFTY"] = replace(chain, contracts=tuple(updated))
    return replace(snapshot, chains=chains)


class OptionTickSmokeRegressionTests(unittest.TestCase):
    def test_a_index_tick_without_option_quote_stays_fail_and_loop_continues(self) -> None:
        catalog = _nifty_catalog()
        index_name, index_id = catalog["mapping"][0]
        self.assertEqual(index_name, "NIFTY 50")
        incoming = [
            _auth_ok(),
            _symbolsadded(catalog["mapping"]),
            _trade(index_id, float(catalog["spots"]["NIFTY"]), seq=1),
        ]
        loop = _start_smoke(incoming, catalog)
        max_cycles = 6
        reports = drain_smoke_loop(loop, max_cycles=max_cycles)
        report = build_smoke_report(loop, reports, root=ROOT)
        self.assertEqual(len(reports), max_cycles)
        self.assertIsNotNone(loop.last_snapshot)
        self.assertTrue(report["connection"]["authenticated"])
        self.assertTrue(report["catalog"]["ok"])
        self.assertTrue(report["subscription"]["mapping_ready"])
        self.assertFalse(report["option_tick_ok"])
        self.assertIsNone(report["first_option_tick"])
        self.assertIsNone(report["first_tick"])
        self.assertEqual(report["result"], FAIL)
        self.assertNotIn(report["result"], {PASS, PASS_WITH_NO_TRADE, HARD_FAIL})
        loop.stop()

    def test_b_index_then_ce_quote_is_a_live_option_tick(self) -> None:
        catalog = _nifty_catalog()
        incoming, (ce_name, ce_id, quote) = _index_then_ce_incoming(catalog)
        loop = _start_smoke(incoming, catalog)
        reports = drain_smoke_loop(loop, max_cycles=16)
        self.assertLess(len(reports), 16)
        self.assertGreaterEqual(len(loop.snapshots), 2)
        self.assertFalse(_assess(loop, loop.snapshots[0]).ok)
        report = build_smoke_report(loop, reports, root=ROOT)
        self.assertIn(report["result"], {PASS, PASS_WITH_NO_TRADE})
        tick = report["first_option_tick"]
        self.assertIsNotNone(tick)
        self.assertEqual(tick["option_type"], "CE")
        self.assertEqual(tick["provider_symbol"], ce_name)
        self.assertEqual(tick["provider_symbol_id"], str(ce_id))
        self.assertEqual(tick["underlying"], "NIFTY")
        self.assertEqual(tick["expiry"], quote["expiry"])
        self.assertEqual(float(tick["strike"]), float(quote["strike"]))
        self.assertEqual(tick["bid"], float(quote["bid"]))
        self.assertEqual(tick["ask"], float(quote["ask"]))
        self.assertEqual(tick["ltp"], float(quote["ltp"]))
        self.assertTrue(tick["quote_freshness"])
        self.assertIn("-CE", tick["canonical_id"])
        self.assertTrue(tick["snapshot_id"])
        self.assertTrue(tick["event_time"])
        self.assertTrue(tick["received_time"])
        self.assertEqual(tick["sequence"], loop.last_snapshot.sequence)
        self.assertNotEqual(tick["provider_symbol"], "NIFTY 50")
        self.assertFalse(report["safety"]["fixture_fallback"])
        loop.stop()

    def test_c_contract_master_without_option_quotes_is_not_a_tick(self) -> None:
        catalog = _nifty_catalog()
        _index_name, index_id = catalog["mapping"][0]
        incoming = [
            _auth_ok(),
            _symbolsadded(catalog["mapping"]),
            _trade(index_id, float(catalog["spots"]["NIFTY"]), seq=1),
        ]
        loop = _start_smoke(incoming, catalog)
        reports = drain_smoke_loop(loop, max_cycles=3)
        snapshot = loop.last_snapshot
        self.assertIsNotNone(snapshot)
        contracts = snapshot.chains["NIFTY"].contracts
        self.assertGreater(len(contracts), 0)
        self.assertEqual(loop.provider._quotes, {})
        for contract in contracts:
            self.assertIn(str(getattr(contract.option_type, "value", contract.option_type)), {"CE", "PE"})
            self.assertIsNone(contract.bid)
            self.assertIsNone(contract.ask)
            self.assertIsNone(contract.last_price)
        check = _assess(loop, snapshot)
        self.assertFalse(check.ok)
        self.assertEqual(check.quotes, ())
        self.assertFalse(check.fixture_rejected)
        report = build_smoke_report(loop, reports, root=ROOT)
        self.assertIsNone(report["first_option_tick"])
        self.assertFalse(report["option_tick_ok"])
        self.assertEqual(report["result"], FAIL)
        loop.stop()

    def test_d_heartbeat_does_not_count_as_option_tick(self) -> None:
        catalog = _nifty_catalog()
        heartbeat = json.dumps({"HeartBeat": {"timestamp": AS_OF.isoformat(), "message": "heartbeat"}})
        incoming = [_auth_ok(), _symbolsadded(catalog["mapping"]), heartbeat]
        loop = _start_smoke(incoming, catalog)
        reports = drain_smoke_loop(loop, max_cycles=5)
        self.assertEqual(len(reports), 5)
        self.assertTrue(any(row.reason == "FEED_HEARTBEAT" for row in reports))
        self.assertIsNone(loop.last_snapshot)
        report = build_smoke_report(loop, reports, root=ROOT)
        self.assertIsNone(report["first_option_tick"])
        self.assertIsNone(report["first_tick"])
        self.assertFalse(report["option_tick_ok"])
        self.assertEqual(report["result"], FAIL)
        self.assertNotIn(report["result"], {PASS, PASS_WITH_NO_TRADE})
        loop.stop()

    def test_e_option_quote_missing_provider_symbol_id_does_not_count(self) -> None:
        catalog = _nifty_catalog()
        incoming, (ce_name, _ce_id, _quote) = _index_then_ce_incoming(catalog)
        loop = _start_smoke(incoming, catalog)
        reports = drain_smoke_loop(loop, max_cycles=8)
        self.assertTrue(_assess(loop).ok)
        loop.provider._symbol_ids = {
            ident: name for ident, name in loop.provider._symbol_ids.items() if name != ce_name
        }
        report = build_smoke_report(loop, reports, root=ROOT)
        self.assertTrue(report["connection"]["authenticated"])
        self.assertTrue(report["catalog"]["ok"])
        self.assertTrue(report["subscription"]["mapping_ready"])
        self.assertFalse(report["option_tick_ok"])
        self.assertIsNone(report["first_option_tick"])
        self.assertIsNone(report["first_tick"])
        self.assertEqual(report["result"], FAIL)
        loop.stop()

    def test_f_fixture_option_quote_is_hard_fail(self) -> None:
        catalog = _nifty_catalog()
        fixture = bullish_event(underlyings=("NIFTY",))
        fixture["is_fixture"] = True
        self.assertTrue(fixture["option_quotes"])
        incoming = [_auth_ok(), _symbolsadded(catalog["mapping"]), json.dumps(fixture)]
        loop = _start_smoke(incoming, catalog)
        reports = drain_smoke_loop(loop, max_cycles=6)
        self.assertLess(len(reports), 6)
        self.assertTrue(any("FIXTURE_FALLBACK" in row.reason for row in reports))
        report = build_smoke_report(loop, reports, root=ROOT)
        self.assertEqual(report["result"], HARD_FAIL)
        self.assertTrue(report["safety"]["fixture_fallback"])
        self.assertIsNone(report["first_option_tick"])
        self.assertFalse(report["option_tick_ok"])
        loop.stop()

        incoming, _ce = _index_then_ce_incoming(catalog)
        loop = _start_smoke(incoming, catalog)
        reports = drain_smoke_loop(loop, max_cycles=8)
        self.assertTrue(loop.provider._quotes)
        for quote in loop.provider._quotes.values():
            quote["is_fixture"] = True
        report = build_smoke_report(loop, reports, root=ROOT)
        self.assertEqual(report["result"], HARD_FAIL)
        self.assertIsNone(report["first_option_tick"])
        self.assertFalse(report["option_tick_ok"])
        for quote in loop.provider._quotes.values():
            quote["is_fixture"] = False
        chain = loop.last_snapshot.chains["NIFTY"]
        loop.last_snapshot = replace(loop.last_snapshot, chains={"NIFTY": replace(chain, is_fixture=True)})
        report = build_smoke_report(loop, reports, root=ROOT)
        self.assertEqual(report["result"], HARD_FAIL)
        self.assertIsNone(report["first_option_tick"])
        self.assertTrue(report["safety"]["fixture_fallback"])
        loop.stop()


class OptionQuoteFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        catalog = _nifty_catalog()
        incoming, _ce = _index_then_ce_incoming(catalog)
        self.loop = _start_smoke(incoming, catalog)
        self.reports = drain_smoke_loop(self.loop, max_cycles=8)
        self.limit = self.loop.config.live_data.max_staleness_seconds
        self.fresh = self.loop.last_snapshot
        self.assertIsNotNone(self.fresh)
        self.assertTrue(self.fresh.freshness_ok)

    def tearDown(self) -> None:
        self.loop.stop()

    def _report_for(self, snapshot):
        self.loop.last_snapshot = snapshot
        return build_smoke_report(self.loop, self.reports, root=ROOT)

    def test_a_fresh_ce_quote_counts(self) -> None:
        check = _assess(self.loop, self.fresh)
        self.assertTrue(check.ok)
        self.assertTrue(check.quotes[0]["quote_freshness"])
        report = self._report_for(self.fresh)
        self.assertTrue(report["option_tick_ok"])
        self.assertIsNotNone(report["first_option_tick"])
        self.assertTrue(report["first_option_tick"]["quote_freshness"])
        self.assertEqual(report["first_option_tick"]["option_type"], "CE")

    def test_b_stale_ce_quote_does_not_count(self) -> None:
        quote_time = AS_OF - timedelta(seconds=self.limit + 60)
        stale = _retimed_ce(self.fresh, quote_time)
        self.assertTrue(stale.freshness_ok)
        check = _assess(self.loop, stale)
        self.assertFalse(check.ok)
        self.assertEqual(check.quotes, ())
        report = self._report_for(stale)
        self.assertFalse(report["option_tick_ok"])
        self.assertIsNone(report["first_option_tick"])
        self.assertEqual(report["result"], FAIL)
        priced = next(
            contract
            for contract in stale.chains["NIFTY"].contracts
            if contract.bid is not None and str(getattr(contract.option_type, "value", contract.option_type)) == "CE"
        )
        self.assertEqual(priced.timestamp, quote_time)

    def test_c_future_ce_quote_does_not_count(self) -> None:
        future = _retimed_ce(self.fresh, AS_OF + timedelta(seconds=1))
        self.assertTrue(future.freshness_ok)
        self.assertFalse(_assess(self.loop, future).ok)
        report = self._report_for(future)
        self.assertFalse(report["option_tick_ok"])
        self.assertIsNone(report["first_option_tick"])
        self.assertEqual(report["result"], FAIL)

    def test_d_ce_quote_at_staleness_boundary_counts(self) -> None:
        boundary = _retimed_ce(self.fresh, AS_OF - timedelta(seconds=self.limit))
        check = _assess(self.loop, boundary)
        self.assertTrue(check.ok)
        self.assertTrue(check.quotes[0]["quote_freshness"])
        report = self._report_for(boundary)
        self.assertTrue(report["option_tick_ok"])
        self.assertTrue(report["first_option_tick"]["quote_freshness"])

    def test_e_ce_quote_beyond_staleness_boundary_does_not_count(self) -> None:
        beyond = _retimed_ce(self.fresh, AS_OF - timedelta(seconds=self.limit + 1))
        self.assertTrue(beyond.freshness_ok)
        self.assertFalse(_assess(self.loop, beyond).ok)
        report = self._report_for(beyond)
        self.assertFalse(report["option_tick_ok"])
        self.assertIsNone(report["first_option_tick"])
        self.assertEqual(report["result"], FAIL)
