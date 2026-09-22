"""Zerodha market-data parser tests. No network. No orders."""

from __future__ import annotations

import ast
import struct
import unittest
from datetime import timedelta
from pathlib import Path

from grow.clock import FrozenClock
from grow.errors import GrowConfigError, GrowLiveTradingDisabled
from grow.execution.lock import inspect_environment
from grow.live_data.kite_market import (
    KiteMarketProvider,
    decode_market_packet,
    parse_nfo_instruments,
    select_option_contract,
)
from grow.live_data.kite_market import ScriptedKiteTransport
from grow.live_data.loop import LivePaperLoop
from grow.live_data.normalize import normalize_event
from grow.live_data.smoke import (
    FAIL,
    HARD_FAIL,
    PASS,
    PASS_WITH_NO_TRADE,
    _option_quote_freshness_failure,
    assess_option_ticks,
    build_smoke_report,
    drain_smoke_loop,
    format_option_tick_evidence,
    kite_market_smoke_config,
)
from grow.market.session import SessionCalendar
from tests.helpers import TEST_RISK_SECRET
from tests.test_live_data import AS_OF, _live_config

ROOT = Path(__file__).resolve().parents[1]
CE_TOKEN = (1000 << 8) | 2
PE_TOKEN = (1001 << 8) | 2
FAR_TOKEN = (1002 << 8) | 2
INDEX_TOKEN = 256265
CSV = """instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange
{ce},390,NIFTY26SEP24200CE,NIFTY,10,2026-09-22,24200,0.05,75,CE,NFO-OPT,NFO
{pe},391,NIFTY26SEP24200PE,NIFTY,12,2026-09-22,24200,0.05,75,PE,NFO-OPT,NFO
{far},392,NIFTY26SEP24000CE,NIFTY,40,2026-09-22,24000,0.05,75,CE,NFO-OPT,NFO
256008,393,NIFTY26SEP24500CE,NIFTY,5,2026-09-29,24500,0.05,75,CE,NFO-OPT,NFO
256010,394,NIFTY26SEP15000CE,NIFTY,1,2026-09-15,24000,0.05,75,CE,NFO-OPT,NFO
256012,395,BANKNIFTY26SEP55000CE,BANKNIFTY,20,2026-09-29,55000,0.05,30,CE,NFO-OPT,NFO
256014,396,RELIANCE26SEP1400CE,RELIANCE,5,2026-09-22,1400,0.05,250,CE,NFO-OPT,NFO
256016,397,NIFTY26SEPFUT,NIFTY,24000,2026-09-29,0,0.05,75,FUT,NFO-FUT,NFO
""".format(ce=CE_TOKEN, pe=PE_TOKEN, far=FAR_TOKEN)


def _frame(packet: bytes) -> bytes:
    return struct.pack(">HH", 1, len(packet)) + packet


def _index_packet(price: float, when) -> bytes:
    buf = bytearray(32)
    struct.pack_into(">I", buf, 0, INDEX_TOKEN)
    struct.pack_into(">I", buf, 4, int(round(price * 100)))
    struct.pack_into(">I", buf, 28, int(when.timestamp()))
    return bytes(buf)


def _full_packet(token: int, *, ltp: float, bid: float, ask: float, volume: int, oi: int, when) -> bytes:
    buf = bytearray(184)
    struct.pack_into(">I", buf, 0, token)
    struct.pack_into(">I", buf, 4, int(round(ltp * 100)))
    struct.pack_into(">I", buf, 16, volume)
    struct.pack_into(">I", buf, 44, int(when.timestamp()))
    struct.pack_into(">I", buf, 48, oi)
    struct.pack_into(">I", buf, 60, int(when.timestamp()))
    struct.pack_into(">I", buf, 64, 15)
    struct.pack_into(">I", buf, 68, int(round(bid * 100)))
    struct.pack_into(">H", buf, 72, 2)
    struct.pack_into(">I", buf, 124, 12)
    struct.pack_into(">I", buf, 128, int(round(ask * 100)))
    struct.pack_into(">H", buf, 132, 1)
    return bytes(buf)


def _provider(frames, *, spot: float = 24210.0) -> KiteMarketProvider:
    transport = ScriptedKiteTransport(instruments_csv=CSV, spots={"NIFTY": spot}, frames=frames)
    provider = KiteMarketProvider(transport=transport, clock=FrozenClock(AS_OF))
    provider.connect()
    return provider


def _normalize(payload):
    cfg = _live_config(provider="kite_market")
    return normalize_event(
        payload,
        now=AS_OF,
        max_staleness_seconds=cfg.live_data.max_staleness_seconds,
        calendar=SessionCalendar(cfg.market, clock=FrozenClock(AS_OF)),
    )


def _loop(provider: KiteMarketProvider) -> LivePaperLoop:
    cfg = _live_config(provider="kite_market", snapshot_interval_seconds=0, session_timeout_seconds=86400)
    loop = LivePaperLoop(cfg, provider, clock=provider.clock, risk_secret=TEST_RISK_SECRET)
    loop.start()
    return loop


class InstrumentFilterTests(unittest.TestCase):
    def test_nifty_and_banknifty_options_only(self) -> None:
        rows = parse_nfo_instruments(CSV)
        underlyings = {row.underlying for row in rows}
        kinds = {row.option_type for row in rows}
        self.assertEqual(underlyings, {"NIFTY", "BANKNIFTY"})
        self.assertEqual(kinds, {"CE", "PE"})
        self.assertTrue(all(row.strike > 0 for row in rows))
        self.assertNotIn("RELIANCE", underlyings)
        self.assertTrue(all("FUT" not in row.tradingsymbol for row in rows))

    def test_ce_pe_and_token_mapping(self) -> None:
        rows = {row.tradingsymbol: row for row in parse_nfo_instruments(CSV)}
        self.assertEqual(rows["NIFTY26SEP24200CE"].option_type, "CE")
        self.assertEqual(rows["NIFTY26SEP24200PE"].option_type, "PE")
        self.assertEqual(rows["NIFTY26SEP24200CE"].instrument_token, CE_TOKEN)
        self.assertNotEqual(rows["NIFTY26SEP24200CE"].instrument_token, rows["NIFTY26SEP24200PE"].instrument_token)

    def test_selects_nearest_nifty_ce(self) -> None:
        from grow.live_data.expiry_class import ExpiryClassifier

        chosen = select_option_contract(
            parse_nfo_instruments(CSV),
            spots={"NIFTY": 24210.0, "BANKNIFTY": 55000.0},
            as_of=AS_OF.date(),
            classifier=ExpiryClassifier(clock=FrozenClock(AS_OF)),
        )
        self.assertEqual(chosen.underlying, "NIFTY")
        self.assertEqual(chosen.option_type, "CE")
        self.assertEqual(chosen.expiry.isoformat(), "2026-09-22")
        self.assertEqual(chosen.strike, 24200.0)
        self.assertEqual(chosen.instrument_token, CE_TOKEN)


class PacketTests(unittest.TestCase):
    def test_full_packet_keeps_exchange_timestamp_and_depth(self) -> None:
        when = AS_OF - timedelta(seconds=2)
        tick = decode_market_packet(_full_packet(CE_TOKEN, ltp=101.5, bid=101.0, ask=102.0, volume=40, oi=80, when=when))
        self.assertEqual(tick.mode, "full")
        self.assertEqual(tick.last_price, 101.5)
        self.assertEqual(tick.bid, 101.0)
        self.assertEqual(tick.ask, 102.0)
        self.assertEqual(tick.volume, 40)
        self.assertEqual(tick.open_interest, 80)
        self.assertEqual(tick.exchange_timestamp, when)
        self.assertIsNotNone(tick.exchange_timestamp.tzinfo)

    def test_ltp_packet_does_not_invent_depth_or_timestamp(self) -> None:
        packet = struct.pack(">II", CE_TOKEN, 10150)
        tick = decode_market_packet(packet)
        self.assertEqual(tick.mode, "ltp")
        self.assertEqual(tick.last_price, 101.5)
        self.assertIsNone(tick.bid)
        self.assertIsNone(tick.ask)
        self.assertIsNone(tick.volume)
        self.assertIsNone(tick.open_interest)
        self.assertIsNone(tick.exchange_timestamp)

    def test_malformed_frame_is_rejected(self) -> None:
        with self.assertRaises(GrowConfigError) as ctx:
            decode_market_packet(b"\x00" * 10)
        self.assertIn("MALFORMED_MESSAGE", str(ctx.exception))


class OptionGateTests(unittest.TestCase):
    def test_index_tick_does_not_pass_the_option_gate(self) -> None:
        provider = _provider([_frame(_index_packet(24210.0, AS_OF))])
        payload = provider.poll()
        self.assertEqual(payload["option_quotes"], [])
        self.assertEqual(payload["spots"]["NIFTY"], 24210.0)
        snapshot = _normalize(payload)
        self.assertTrue(snapshot.freshness_ok)
        loop = _loop(provider)
        loop.last_snapshot = snapshot
        report = build_smoke_report(loop, [], root=ROOT)
        self.assertFalse(report["option_tick_ok"])
        self.assertIsNone(report["first_option_tick"])
        self.assertEqual(report["result"], FAIL)
        loop.stop()

    def test_fresh_ce_quote_passes_existing_validation(self) -> None:
        when = AS_OF - timedelta(seconds=2)
        provider = _provider(
            [
                _frame(_index_packet(24210.0, AS_OF)),
                _frame(_full_packet(CE_TOKEN, ltp=101.5, bid=101.0, ask=102.0, volume=40, oi=80, when=when)),
            ]
        )
        self.assertEqual(provider.selected.instrument_token, CE_TOKEN)
        self.assertIn(CE_TOKEN, provider.transport.subscribed)
        self.assertIn(INDEX_TOKEN, provider.transport.subscribed)
        self.assertEqual(provider.transport.mode, "full")
        self.assertEqual(provider.poll()["option_quotes"], [])
        payload = provider.poll()
        snapshot = _normalize(payload)
        check = assess_option_ticks(
            snapshot,
            provider._symbol_ids,
            max_staleness_seconds=30,
            now=AS_OF,
        )
        self.assertTrue(check.ok)
        self.assertEqual(check.quotes[0]["option_type"], "CE")
        self.assertEqual(check.rejection_count, 0)
        loop = _loop(provider)
        loop.last_snapshot = snapshot
        report = build_smoke_report(loop, [], root=ROOT)
        self.assertTrue(report["option_tick_ok"])
        tick = report["first_option_tick"]
        self.assertEqual(tick["option_type"], "CE")
        self.assertEqual(tick["provider_symbol"], "NIFTY26SEP24200CE")
        self.assertEqual(tick["provider_symbol_id"], str(CE_TOKEN))
        self.assertEqual(tick["canonical_id"], "NIFTY-2026-09-22-24200-CE")
        self.assertEqual(tick["quote_timestamp"], when.isoformat())
        self.assertNotEqual(tick["quote_timestamp"], tick["received_time"])
        self.assertEqual(tick["ltp"], 101.5)
        self.assertEqual(tick["bid"], 101.0)
        self.assertEqual(tick["ask"], 102.0)
        self.assertTrue(tick["quote_freshness"])
        self.assertIsNone(report["option_tick_rejection"])
        self.assertIn(report["result"], {PASS, PASS_WITH_NO_TRADE})
        evidence = format_option_tick_evidence(report)
        self.assertIn("OPTION TICK: PASS", evidence)
        self.assertIn("Provider: Zerodha", evidence)
        self.assertIn(f"Instrument token: {CE_TOKEN}", evidence)
        loop.stop()

    def test_quote_without_exchange_timestamp_is_not_accepted(self) -> None:
        packet = bytearray(44)
        struct.pack_into(">I", packet, 0, CE_TOKEN)
        struct.pack_into(">I", packet, 4, 10150)
        provider = _provider([_frame(bytes(packet))])
        payload = provider.poll()
        self.assertEqual(payload["kind"], "control")
        self.assertIsNone(provider._quote)

    def test_order_text_and_malformed_frame_do_not_become_quotes(self) -> None:
        provider = _provider(
            [
                '{"type":"order","data":{"status":"COMPLETE"}}',
                _frame(b"\x00" * 10),
            ]
        )
        first = provider.poll()
        second = provider.poll()
        self.assertEqual(first["reason"], "ORDER_IGNORED")
        self.assertEqual(second["reason"], "MALFORMED_MESSAGE")
        self.assertIsNone(provider._quote)

    def test_unmapped_token_does_not_count(self) -> None:
        when = AS_OF - timedelta(seconds=2)
        provider = _provider([_frame(_full_packet(FAR_TOKEN, ltp=10, bid=9, ask=11, volume=1, oi=1, when=when))])
        payload = provider.poll()
        self.assertEqual(payload["kind"], "control")
        self.assertIsNone(provider._quote)

    def test_stale_exchange_timestamp_is_not_a_tick(self) -> None:
        when = AS_OF - timedelta(seconds=90)
        self.assertEqual(
            _option_quote_freshness_failure(when, when, AS_OF, 30),
            "stale_option_quote",
        )
        provider = _provider([_frame(_full_packet(CE_TOKEN, ltp=10, bid=9, ask=11, volume=1, oi=1, when=when))])
        payload = provider.poll()
        self.assertEqual(payload["event_time"], when.isoformat())
        self.assertEqual(payload["option_quotes"][0]["ts"], when.isoformat())
        cfg = _live_config(provider="kite_market")
        snapshot = normalize_event(
            payload,
            now=AS_OF,
            max_staleness_seconds=30,
            calendar=SessionCalendar(cfg.market, clock=FrozenClock(AS_OF)),
        )
        self.assertFalse(snapshot.freshness_ok)
        loop = _loop(provider)
        loop.last_snapshot = snapshot
        report = build_smoke_report(loop, [], root=ROOT)
        self.assertFalse(report["option_tick_ok"])
        self.assertIsNone(report["first_option_tick"])
        loop.stop()

    def test_future_exchange_timestamp_is_rejected(self) -> None:
        when = AS_OF + timedelta(seconds=5)
        self.assertEqual(
            _option_quote_freshness_failure(when, AS_OF, AS_OF, 30),
            "future_option_quote",
        )
        provider = _provider([_frame(_full_packet(CE_TOKEN, ltp=10, bid=9, ask=11, volume=1, oi=1, when=when))])
        payload = provider.poll()
        self.assertEqual(payload["option_quotes"][0]["ts"], when.isoformat())
        self.assertEqual(payload["event_time"], when.isoformat())
        with self.assertRaises(GrowConfigError) as ctx:
            _normalize(payload)
        self.assertTrue(
            "FUTURE_SNAPSHOT" in str(ctx.exception) or "TIMESTAMP_INVERTED" in str(ctx.exception)
        )

    def test_fixture_message_is_hard_fail(self) -> None:
        provider = _provider(['{"is_fixture": true}'])
        loop = _loop(provider)
        reports = drain_smoke_loop(loop, max_cycles=4)
        self.assertTrue(any("FIXTURE_FALLBACK" in row.reason for row in reports))
        report = build_smoke_report(loop, reports, root=ROOT)
        self.assertEqual(report["result"], HARD_FAIL)
        self.assertIsNone(report["first_option_tick"])
        self.assertFalse(report["option_tick_ok"])
        loop.stop()


class ZerodhaSmokeSafetyTests(unittest.TestCase):
    def test_smoke_config_is_paper_only(self) -> None:
        cfg = kite_market_smoke_config(
            {
                "ZERODHA_SMOKE": "1",
                "KITE_API_KEY": "market-key",
                "KITE_ACCESS_TOKEN": "market-token-value",
                "GROW_RISK_SECRET": TEST_RISK_SECRET,
            }
        )
        self.assertEqual(cfg.live_data.provider, "kite_market")
        self.assertTrue(cfg.live_data.paper_mode)
        self.assertFalse(cfg.live_data.live_trading)
        self.assertFalse(cfg.execution.live_trading_enabled)
        with self.assertRaises(GrowLiveTradingDisabled):
            inspect_environment({"KITE_ACCESS_TOKEN": "market-token-value"})

    def test_live_trading_flag_still_refuses(self) -> None:
        with self.assertRaises(GrowLiveTradingDisabled):
            kite_market_smoke_config({"ZERODHA_SMOKE": "1", "GROW_LIVE_TRADING": "true", "GROW_RISK_SECRET": TEST_RISK_SECRET})

    def test_script_has_no_order_route_or_injected_ticks(self) -> None:
        text = (ROOT / "scripts" / "run_zerodha_smoke.py").read_text(encoding="utf-8")
        market = (ROOT / "grow" / "live_data" / "kite_market.py").read_text(encoding="utf-8")
        for source in (text, market):
            self.assertNotIn("place_order", source)
            self.assertNotIn("/orders", source)
            self.assertNotIn("kiteconnect", source.lower())
        self.assertNotIn("frames", text)
        tree = ast.parse(text)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertTrue({"place_order", "LiveBroker", "kiteconnect"}.isdisjoint(names))

    def test_main_without_flag_is_disabled(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("run_zerodha_smoke", ROOT / "scripts" / "run_zerodha_smoke.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        import io
        from contextlib import redirect_stderr

        buf = io.StringIO()
        with redirect_stderr(buf):
            code = mod.main({"PATH": "/usr/bin"})
        self.assertEqual(code, 2)
        self.assertIn("SMOKE_DISABLED", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
