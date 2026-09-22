from __future__ import annotations

import ast
import json
import pathlib
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta

from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.errors import GrowConfigError
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.history.resolver import DISALLOWED_CLASS, NO_ELIGIBLE_EXPIRY, resolve_nearest_expiry
from grow.history.universe import (
    IndexPolicy,
    IndexUniverseRegistry,
    MONTHLY_ONLY,
    WEEKLY_PREFERRED,
    default_index_policies,
)
from grow.live_data.catalog import UNKNOWN_EXPIRY_CLASS, normalize_catalog_row, parse_catalog_text, merge_catalog
from grow.live_data.loop import LivePaperLoop
from grow.live_data.mock import bullish_event
from grow.live_data.models import TRUEDATA_PROVIDER_ID, CycleStatus, SessionHealth
from grow.live_data.normalize import normalize_event
from grow.live_data.provider import open_provider
from grow.live_data.subscribe import plan_subscriptions
from grow.live_data.symbols import parse_provider_symbol, parse_tick_fields
from grow.live_data.truedata import ReplayTransport, TrueDataAdapter, TrueDataSettings, load_truedata_secrets
from grow.market.session import SessionCalendar
from grow.paper.positions import PositionState
from tests.helpers import TEST_RISK_SECRET
from tests.test_live_data import AS_OF, _live_config
from tests.test_positions import _set_quotes


ROOT = pathlib.Path(__file__).resolve().parents[1]
CAPTURED = ROOT / "tests" / "fixtures" / "truedata" / "sample_ticks.json"


def _td_event(sequence: int = 1, **kwargs):
    payload = bullish_event(sequence=sequence, **kwargs)
    payload["provider"] = TRUEDATA_PROVIDER_ID
    return payload


def _settings(**kwargs) -> TrueDataSettings:
    defaults = dict(mode="replay", reconnect_policy="fail_closed", max_symbols=50, strike_window=4)
    defaults.update(kwargs)
    return TrueDataSettings(**defaults)


def _adapter(events, **kwargs) -> TrueDataAdapter:
    clock = kwargs.pop("clock", FrozenClock(AS_OF))
    adapter = TrueDataAdapter(events=tuple(events), settings=_settings(**kwargs.pop("settings_kw", {})), clock=clock, **kwargs)
    adapter.connect()
    return adapter


def _td_loop(events, config=None, **adapter_kw) -> LivePaperLoop:
    cfg = config or _live_config(provider="truedata", snapshot_interval_seconds=0, session_timeout_seconds=86400)
    clock = FrozenClock(AS_OF)
    adapter = TrueDataAdapter(
        events=tuple(events),
        settings=_settings(),
        clock=clock,
        **adapter_kw,
    )
    loop = LivePaperLoop(cfg, adapter, clock=clock, risk_secret=TEST_RISK_SECRET)
    loop.start()
    return loop


def _fin_policy() -> IndexPolicy:
    return IndexPolicy(
        index_id="FINNIFTY",
        canonical_symbol="FINNIFTY",
        display_name="Nifty Financial Services",
        exchange="NSE",
        instrument_type="OPTIDX",
        active_from=date(2020, 1, 1),
        active_to=None,
        option_supported=True,
        expiry_policy_profile=MONTHLY_ONLY,
        strike_policy_profile="ATM_PM2",
        lot_size_source="CONTRACT_MASTER",
        liquidity_policy="options.select.v1",
        research_status="APPROVED",
        license_status="POLICY",
        provider_symbol_map=(("canonical", "FINNIFTY"),),
    )


class SymbolParseTests(unittest.TestCase):
    def test_option_and_index_mapping(self) -> None:
        ce = parse_provider_symbol("NIFTY25092225000CE")
        self.assertEqual(ce.canonical_symbol, "NIFTY")
        self.assertEqual(ce.expiry, date(2025, 9, 22))
        self.assertEqual(ce.strike, 25000.0)
        self.assertEqual(ce.option_type, "CE")
        self.assertNotEqual(ce.canonical_contract_id(), ce.provider_symbol)
        pe = parse_provider_symbol("BANKNIFTY21031834500PE")
        self.assertEqual(pe.canonical_symbol, "BANKNIFTY")
        self.assertEqual(pe.option_type, "PE")
        self.assertEqual(parse_provider_symbol("NIFTY 50").canonical_symbol, "NIFTY")
        self.assertEqual(parse_provider_symbol("NIFTY BANK").instrument_type, "INDEX")

    def test_tick_csv_does_not_fabricate_bid(self) -> None:
        row = parse_tick_fields("NIFTY 50,2026-09-18T11:00:00+05:30,25040,1,25040,10,25000,25100,24900,24950,0,0,0,,3,,,,,")
        self.assertEqual(row["ltp"], 25040.0)
        self.assertIsNone(row["bid"])
        self.assertIsNone(row["ask"])
        self.assertIsNone(row["iv"])
        captured = json.loads(CAPTURED.read_text(encoding="utf-8"))
        csv_line = captured["ticks"][2]
        parsed = parse_tick_fields(csv_line)
        self.assertEqual(parsed["bid"], 69.5)
        self.assertEqual(parsed["sequence"], 12)


class AuthAndFactoryTests(unittest.TestCase):
    def test_missing_credentials_fail_closed(self) -> None:
        with self.assertRaises(GrowConfigError) as ctx:
            load_truedata_secrets(environ={})
        self.assertIn("AUTH_MISSING", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            TrueDataAdapter(environ={}, settings=TrueDataSettings(mode="real"))
        self.assertIn("AUTH_MISSING", str(ctx.exception))

    def test_auth_rejected(self) -> None:
        adapter = TrueDataAdapter(
            transport=ReplayTransport(login_ok=False),
            settings=_settings(),
            clock=FrozenClock(AS_OF),
        )
        with self.assertRaises(GrowConfigError) as ctx:
            adapter.connect()
        self.assertIn("AUTH_FAILED", str(ctx.exception))
        self.assertEqual(adapter.health().state, SessionHealth.STOPPED)

    def test_open_provider_approves_truedata_replay(self) -> None:
        provider = open_provider("truedata", events=(_td_event(),), settings=_settings())
        self.assertEqual(provider.identity, TRUEDATA_PROVIDER_ID)


class DiscoveryTests(unittest.TestCase):
    def test_dynamic_index_discovery_uses_policy_overlay(self) -> None:
        catalog = [
            {
                "provider_symbol": "NIFTY25092225000CE",
                "canonical_symbol": "NIFTY",
                "instrument_type": "INDEX_OPTION",
                "expiry": date(2026, 9, 22),
                "strike": 25000,
                "option_type": "CE",
                "lot_size": 75,
                "expiry_class": "WEEKLY",
            },
            {
                "provider_symbol": "FINNIFTY25092225000CE",
                "canonical_symbol": "FINNIFTY",
                "instrument_type": "INDEX_OPTION",
                "expiry": date(2026, 9, 22),
                "strike": 25000,
                "option_type": "CE",
                "lot_size": 40,
                "expiry_class": "WEEKLY",
            },
        ]
        default_adapter = TrueDataAdapter(events=(), catalog=catalog, settings=_settings(), clock=FrozenClock(AS_OF))
        self.assertEqual(default_adapter.discover_underlyings(), ("NIFTY",))
        overlay = IndexUniverseRegistry(default_index_policies() + (_fin_policy(),))
        allowed = TrueDataAdapter(
            events=(),
            catalog=catalog,
            settings=_settings(),
            clock=FrozenClock(AS_OF),
            registry=overlay,
        )
        self.assertEqual(allowed.discover_underlyings(), ("NIFTY", "FINNIFTY"))
        self.assertNotEqual(overlay.fingerprint, default_adapter.registry.fingerprint)

    def test_same_day_expiry_is_not_selected(self) -> None:
        catalog = [
            {
                "provider_symbol": "NIFTY 50",
                "canonical_symbol": "NIFTY",
                "instrument_type": "INDEX",
                "expiry": None,
                "strike": None,
                "option_type": None,
                "lot_size": None,
                "expiry_class": None,
            },
            {
                "provider_symbol": "NIFTY26091825000CE",
                "canonical_symbol": "NIFTY",
                "instrument_type": "INDEX_OPTION",
                "expiry": date(2026, 9, 18),
                "strike": 25000,
                "option_type": "CE",
                "lot_size": 75,
                "expiry_class": "WEEKLY",
            },
            {
                "provider_symbol": "NIFTY26092225000CE",
                "canonical_symbol": "NIFTY",
                "instrument_type": "INDEX_OPTION",
                "expiry": date(2026, 9, 22),
                "strike": 25000,
                "option_type": "CE",
                "lot_size": 75,
                "expiry_class": "WEEKLY",
            },
        ]
        adapter = TrueDataAdapter(events=(), catalog=catalog, settings=_settings(), clock=FrozenClock(AS_OF))
        adapter._spots["NIFTY"] = 25000.0
        adapter.connect()
        self.assertTrue(any("NIFTY26092225000CE" in s for s in adapter._desired))
        self.assertFalse(any("NIFTY26091825000CE" in s for s in adapter._desired))


class QualityGateTests(unittest.TestCase):
    def test_future_and_stale_and_order(self) -> None:
        calendar = SessionCalendar(load_config().market, clock=FrozenClock(AS_OF))
        future = _td_event()
        future["event_time"] = (AS_OF + timedelta(minutes=5)).isoformat()
        future["received_time"] = (AS_OF + timedelta(minutes=5)).isoformat()
        with self.assertRaises(GrowConfigError) as ctx:
            normalize_event(future, now=AS_OF, max_staleness_seconds=30, calendar=calendar)
        self.assertIn("FUTURE_SNAPSHOT", str(ctx.exception))
        stale = _td_event()
        stale["event_time"] = (AS_OF - timedelta(minutes=5)).isoformat()
        stale["received_time"] = AS_OF.isoformat()
        snap = normalize_event(stale, now=AS_OF, max_staleness_seconds=30, calendar=calendar)
        self.assertFalse(snap.freshness_ok)
        adapter = _adapter([_td_event(sequence=2), _td_event(sequence=1)])
        adapter.poll()
        with self.assertRaises(GrowConfigError) as ctx:
            adapter.poll()
        self.assertIn("OUT_OF_ORDER", str(ctx.exception))

    def test_duplicate_tick_sequence(self) -> None:
        ticks = [
            {
                "kind": "tick",
                "symbol": "NIFTY25092225000CE",
                "timestamp": AS_OF.isoformat(),
                "ltp": 80,
                "bid": 79,
                "ask": 81,
                "sequence": 4,
            },
            {
                "kind": "tick",
                "symbol": "NIFTY25092225000CE",
                "timestamp": AS_OF.isoformat(),
                "ltp": 81,
                "bid": 80,
                "ask": 82,
                "sequence": 4,
            },
        ]
        adapter = _adapter(ticks)
        adapter.poll()
        with self.assertRaises(GrowConfigError) as ctx:
            adapter.poll()
        self.assertIn("DUPLICATE_SEQUENCE", str(ctx.exception))


class SubscriptionTests(unittest.TestCase):
    def test_subscription_cap_drops_far_strikes(self) -> None:
        instruments = []
        for k in range(-6, 7):
            strike = 25000 + k * 50
            for kind in ("CE", "PE"):
                instruments.append(
                    {
                        "provider_symbol": f"NIFTY250922{strike}{kind}",
                        "canonical_symbol": "NIFTY",
                        "expiry": date(2026, 9, 22),
                        "strike": float(strike),
                        "option_type": kind,
                    }
                )
        instruments.append(
            {
                "provider_symbol": "NIFTY 50",
                "canonical_symbol": "NIFTY",
                "expiry": None,
                "strike": None,
                "option_type": None,
            }
        )
        planned = plan_subscriptions(
            instruments=instruments,
            spots={"NIFTY": 25010.0},
            selected_expiry={"NIFTY": date(2026, 9, 22)},
            strike_window=4,
            max_symbols=5,
            active_underlyings=("NIFTY",),
        )
        self.assertLessEqual(len(planned), 5)
        self.assertIn("NIFTY 50", planned)

    def test_reconnect_resubscribes_desired_set(self) -> None:
        transport = ReplayTransport(events=(_td_event(), _td_event(sequence=2)))
        adapter = TrueDataAdapter(
            transport=transport,
            settings=_settings(reconnect_policy="bounded_backoff", max_attempts=3, max_backoff_seconds=8),
            clock=FrozenClock(AS_OF),
        )
        adapter.connect()
        adapter.subscribe(("NIFTY 50", "NIFTY25092225000CE"))
        desired = adapter._desired
        first = adapter.poll()
        self.assertIsNotNone(first)
        transport.fail_next = True
        none = adapter.poll()
        self.assertIsNone(none)
        adapter.clock.advance(timedelta(seconds=2))
        adapter.poll()
        self.assertGreaterEqual(adapter.reconnect_count, 1)
        self.assertEqual(tuple(transport.subscribed), desired)


class MappingAndLotTests(unittest.TestCase):
    def test_lot_size_from_catalog_not_symbol(self) -> None:
        event = _td_event()
        snap = normalize_event(
            event,
            now=AS_OF,
            max_staleness_seconds=30,
            calendar=SessionCalendar(load_config().market, clock=FrozenClock(AS_OF)),
        )
        chain = snap.chains["NIFTY"]
        contract = chain.contracts[0]
        self.assertNotEqual(contract.provider_contract_id, f"{contract.underlying}-{contract.expiry.isoformat()}-{int(contract.strike)}-{contract.option_type.value}")
        self.assertIn(contract.provider_contract_id, snap.lot_sizes)
        self.assertGreaterEqual(snap.lot_sizes[contract.provider_contract_id], 1)

    def test_missing_lot_size_is_no_trade(self) -> None:
        loop = _td_loop([_td_event(missing_lot=True)])
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertEqual(report.reason, "MISSING_LOT_SIZE")

    def test_iv_not_fabricated(self) -> None:
        event = _td_event()
        snap = normalize_event(
            event,
            now=AS_OF,
            max_staleness_seconds=30,
            calendar=SessionCalendar(load_config().market, clock=FrozenClock(AS_OF)),
        )
        contract = snap.chains["NIFTY"].contracts[0]
        self.assertIsNone(contract.implied_volatility)
        self.assertIsNone(contract.delta)


class PipelineTests(unittest.TestCase):
    def test_truedata_snapshot_opens_and_closes_paper(self) -> None:
        loop = _td_loop(
            [
                _td_event(sequence=1),
                _set_quotes(_td_event(sequence=2), bid=10.0, ask=11.0),
            ]
        )
        opened = loop.run_once("NIFTY")[0]
        self.assertEqual(opened.status, CycleStatus.PAPER_FILL, opened.reason)
        self.assertEqual(loop.provider.identity, TRUEDATA_PROVIDER_ID)
        closed = loop.run_once("NIFTY")[0]
        self.assertEqual(closed.status, CycleStatus.PAPER_CLOSE, closed.reason)
        pos = loop.positions.all()[0]
        self.assertEqual(pos.state, PositionState.CLOSED)
        summary = loop.positions.summary()
        self.assertEqual(summary.net_realized_pnl, round(summary.gross_realized_pnl - summary.total_costs, 4))

    def test_fixture_fallback_forbidden(self) -> None:
        loop = _td_loop([{"provider": "grow.data.fixture.v1", "is_fixture": True, "sequence": 1}])
        report = loop.run_once("NIFTY")[0]
        self.assertIn("FIXTURE_FALLBACK_FORBIDDEN", report.reason)

    def test_no_broker_surface(self) -> None:
        self.assertFalse(LIVE_TRADING_COMPILED)
        text = (ROOT / "grow" / "live_data" / "truedata.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        banned = {"place_order", "placeOrder", "submit_order", "modify_order", "cancel_order"}
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertTrue(banned.isdisjoint(names))
        self.assertNotIn("kiteconnect", text.lower())
        self.assertNotIn("smartapi", text.lower())


class ConfigTests(unittest.TestCase):
    def test_default_config_stays_mock_and_disabled(self) -> None:
        cfg = load_config()
        self.assertEqual(cfg.live_data.provider, "mock")
        self.assertFalse(cfg.live_data.enabled)
        self.assertFalse(cfg.live_data.live_trading)
        self.assertTrue(cfg.live_data.paper_mode)
        self.assertEqual(cfg.live_data.mode, "replay")

    def test_truedata_provider_is_allowed(self) -> None:
        cfg = _live_config(provider="truedata", reconnect_policy="bounded_backoff")
        cfg.assert_safe()
        self.assertEqual(cfg.live_data.provider, "truedata")


class ScriptedSocket:
    def __init__(self, incoming: list[str]):
        self.incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    def send(self, data: str) -> None:
        self.sent.append(data)

    def recv(self) -> str:
        if not self.incoming:
            return ""
        return self.incoming.pop(0)

    def close(self) -> None:
        self.closed = True


def _auth_ok() -> str:
    return '{"success": true, "message": "TrueData Real Time Data Service"}'


def _symbolsadded(pairs) -> str:
    return json.dumps({"success": True, "message": "symbols added", "symbolsadded": pairs})


def _td_symbol(underlying: str, expiry, strike, kind: str) -> str:
    day = date.fromisoformat(expiry) if isinstance(expiry, str) else expiry
    return f"{underlying}{day.strftime('%y%m%d')}{int(strike)}{kind}"


def _nifty_catalog():
    event = bullish_event(underlyings=("NIFTY",))
    instruments = []
    mapping = []
    ident = 100
    instruments.append(
        {
            "provider_symbol": "NIFTY 50",
            "canonical_symbol": "NIFTY",
            "instrument_type": "INDEX",
            "lot_size": None,
            "provider_symbol_id": str(ident),
        }
    )
    mapping.append(["NIFTY 50", ident])
    ident += 1
    for row in event["contract_master"]:
        if row["underlying"] != "NIFTY":
            continue
        symbol = _td_symbol(row["underlying"], row["expiry"], row["strike"], row["option_type"])
        instruments.append(
            {
                "provider_symbol": symbol,
                "canonical_symbol": "NIFTY",
                "instrument_type": "INDEX_OPTION",
                "expiry": row["expiry"],
                "strike": row["strike"],
                "option_type": row["option_type"],
                "lot_size": row["lot_size"],
                "expiry_class": row["expiry_class"],
                "provider_symbol_id": str(ident),
            }
        )
        mapping.append([symbol, ident])
        ident += 1
    quotes = [q for q in event["option_quotes"] if q["underlying"] == "NIFTY"]
    return {
        "instruments": instruments,
        "spot_bars": event["spot_bars"],
        "mapping": mapping,
        "quotes": quotes,
        "spots": event["spots"],
    }


def _trade(symbol_id: int, ltp: float, *, bid=None, ask=None, seq: int, oi=5000, volume=200) -> str:
    bid_v = ltp - 0.5 if bid is None else bid
    ask_v = ltp + 0.5 if ask is None else ask
    return json.dumps(
        {
            "trade": [
                str(symbol_id),
                AS_OF.isoformat(),
                str(ltp),
                "1",
                str(ltp),
                str(volume),
                str(ltp),
                str(ltp),
                str(ltp),
                str(ltp),
                str(oi),
                "0",
                "0",
                "",
                str(seq),
                str(bid_v),
                "10",
                str(ask_v),
                "10",
            ]
        }
    )


def _real_adapter(incoming: list[str], catalog, **kwargs):
    socket = ScriptedSocket(incoming)
    adapter = TrueDataAdapter(
        settings=TrueDataSettings(mode="real", reconnect_policy=kwargs.pop("reconnect_policy", "fail_closed"), max_staleness_seconds=30),
        environ={"TRUEDATA_USERNAME": "user", "TRUEDATA_PASSWORD": "pass"},
        socket_factory=lambda: socket,
        catalog_loader=lambda: catalog,
        clock=kwargs.pop("clock", FrozenClock(AS_OF)),
        **kwargs,
    )
    return adapter, socket


class ProtocolDecoderTests(unittest.TestCase):
    def test_classifies_vendor_frames(self) -> None:
        from grow.live_data.protocol import decode_truedata_message

        self.assertEqual(decode_truedata_message(_auth_ok()).kind, "auth")
        self.assertTrue(decode_truedata_message(_auth_ok()).ok)
        fail = decode_truedata_message('{"success": false, "message": "invalid login"}')
        self.assertEqual(fail.kind, "auth")
        self.assertFalse(fail.ok)
        hb = decode_truedata_message('{"HeartBeat": {"timestamp": "2026-09-18T11:00:00+05:30", "message": "heartbeat"}}')
        self.assertEqual(hb.kind, "heartbeat")
        sub = decode_truedata_message('{"success": true, "message": "symbols added", "symbolsadded": [["NIFTY 50", 101]]}')
        self.assertEqual(sub.kind, "subscribe")
        self.assertEqual(sub.payload["mapping"]["101"], "NIFTY 50")
        trade = decode_truedata_message(_trade(101, 25040, seq=1))
        self.assertEqual(trade.kind, "tick")
        err = decode_truedata_message('{"error": "limit exceeded"}')
        self.assertEqual(err.kind, "error")
        disc = decode_truedata_message('{"message": "user disconnected"}')
        self.assertEqual(disc.kind, "disconnect")
        with self.assertRaises(GrowConfigError) as ctx:
            decode_truedata_message("{not-json")
        self.assertIn("MALFORMED_MESSAGE", str(ctx.exception))


class RealTransportTests(unittest.TestCase):
    def test_auth_failure_on_real_transport(self) -> None:
        adapter, _sock = _real_adapter(
            ['{"success": false, "message": "unauthorized"}'],
            {"instruments": [{"provider_symbol": "NIFTY 50", "lot_size": None}]},
        )
        with self.assertRaises(GrowConfigError) as ctx:
            adapter.connect()
        self.assertIn("AUTH_FAILED", str(ctx.exception))

    def test_heartbeat_does_not_trade_or_advance_sequence(self) -> None:
        catalog = _nifty_catalog()
        adapter, _sock = _real_adapter(
            [_auth_ok(), '{"HeartBeat": {"timestamp": "' + AS_OF.isoformat() + '", "message": "heartbeat"}}'],
            catalog,
        )
        adapter.connect()
        before_seq = adapter._adapter_seq
        before_quotes = dict(adapter._quotes)
        raw = adapter.poll()
        self.assertEqual(raw["kind"], "heartbeat")
        self.assertIsNotNone(adapter.health().last_heartbeat_at)
        self.assertEqual(adapter._adapter_seq, before_seq)
        self.assertEqual(adapter._quotes, before_quotes)

    def test_symbol_id_mapping_and_unknown_id(self) -> None:
        catalog = _nifty_catalog()
        index_id = catalog["mapping"][0][1]
        incoming = [
            _auth_ok(),
            '{"success": true, "message": "symbols added", "symbolsadded": ' + str(catalog["mapping"]).replace("'", '"') + "}",
            _trade(index_id, 25040.0, seq=1),
            _trade(999999, 80.0, seq=2),
        ]
        adapter, _sock = _real_adapter(incoming, catalog)
        adapter.connect()
        self.assertFalse(adapter._mapping_ready)
        ack = adapter.poll()
        self.assertEqual(ack["kind"], "subscribe")
        self.assertEqual(adapter._symbol_ids[str(index_id)], "NIFTY 50")
        self.assertTrue(adapter._mapping_ready)
        snap = adapter.poll()
        self.assertIsNotNone(snap)
        self.assertEqual(snap["spots"]["NIFTY"], 25040.0)
        with self.assertRaises(GrowConfigError) as ctx:
            adapter.poll()
        self.assertIn("UNKNOWN_SYMBOL_ID", str(ctx.exception))

    def test_csv_tick_resolves_numeric_id(self) -> None:
        catalog = _nifty_catalog()
        index_id = catalog["mapping"][0][1]
        csv = (
            f"{index_id},{AS_OF.isoformat()},25041,1,25041,10,25000,25100,24900,24950,0,0,0,,8,25040.5,10,25041.5,10"
        )
        incoming = [
            _auth_ok(),
            '{"success": true, "message": "symbols added", "symbolsadded": ' + str(catalog["mapping"]).replace("'", '"') + "}",
            csv,
        ]
        adapter, _sock = _real_adapter(incoming, catalog)
        adapter.connect()
        ack = adapter.poll()
        self.assertEqual(ack["kind"], "subscribe")
        snap = adapter.poll()
        self.assertEqual(snap["spots"]["NIFTY"], 25041.0)
        self.assertNotEqual(snap["spots"]["NIFTY"], index_id)

    def test_provider_error_and_malformed(self) -> None:
        catalog = _nifty_catalog()
        adapter, _sock = _real_adapter([_auth_ok(), '{"error": "upstream"}'], catalog)
        adapter.connect()
        with self.assertRaises(GrowConfigError) as ctx:
            adapter.poll()
        self.assertIn("PROVIDER_ERROR", str(ctx.exception))
        adapter2, _sock2 = _real_adapter([_auth_ok(), "{nope"], catalog)
        adapter2.connect()
        with self.assertRaises(GrowConfigError) as ctx:
            adapter2.poll()
        self.assertIn("MALFORMED_MESSAGE", str(ctx.exception))

    def test_catalog_failure_is_degraded(self) -> None:
        adapter = TrueDataAdapter(
            settings=TrueDataSettings(mode="real"),
            environ={"TRUEDATA_USERNAME": "user", "TRUEDATA_PASSWORD": "pass"},
            socket_factory=lambda: ScriptedSocket([_auth_ok()]),
            catalog_loader=lambda: (_ for _ in ()).throw(GrowConfigError("METADATA_UNAVAILABLE")),
            clock=FrozenClock(AS_OF),
        )
        with self.assertRaises(GrowConfigError) as ctx:
            adapter.connect()
        self.assertIn("METADATA_UNAVAILABLE", str(ctx.exception))
        self.assertEqual(adapter.health().state, SessionHealth.DEGRADED)

    def test_empty_catalog_is_degraded(self) -> None:
        adapter, _sock = _real_adapter([_auth_ok()], {"instruments": []})
        with self.assertRaises(GrowConfigError) as ctx:
            adapter.connect()
        self.assertIn("METADATA_UNAVAILABLE", str(ctx.exception))
        self.assertEqual(adapter.health().state, SessionHealth.DEGRADED)
        self.assertFalse(adapter.instrument_catalog())

    def test_subscribes_index_until_live_spot(self) -> None:
        catalog = dict(_nifty_catalog())
        catalog["spot_bars"] = []
        index_id = catalog["mapping"][0][1]
        spot = float(catalog["spots"]["NIFTY"])
        incoming = [_auth_ok(), _symbolsadded([catalog["mapping"][0]]), _trade(index_id, spot, seq=1)]
        adapter, _sock = _real_adapter(incoming, catalog)
        adapter.connect()
        self.assertIn("NIFTY 50", adapter._desired)
        self.assertFalse(any(symbol.endswith(("CE", "PE")) for symbol in adapter._desired))
        self.assertFalse(adapter._mapping_ready)
        ack = adapter.poll()
        self.assertEqual(ack["kind"], "subscribe")
        self.assertTrue(adapter._mapping_ready)
        snap = adapter.poll()
        self.assertIsNotNone(snap)
        self.assertEqual(snap["spots"]["NIFTY"], spot)
        self.assertTrue(any(symbol.endswith("CE") for symbol in adapter._desired))
        self.assertFalse(adapter._mapping_ready)

    def test_stale_without_heartbeat_or_ticks(self) -> None:
        catalog = _nifty_catalog()
        adapter, _sock = _real_adapter(
            [_auth_ok(), '{"HeartBeat": {"timestamp": "' + AS_OF.isoformat() + '", "message": "heartbeat"}}'],
            catalog,
        )
        adapter.connect()
        adapter.poll()
        adapter.clock.advance(timedelta(seconds=31))
        with self.assertRaises(GrowConfigError) as ctx:
            adapter.poll()
        self.assertIn("STALE_REQUIRED_QUOTE", str(ctx.exception))
        self.assertEqual(adapter.health().state, SessionHealth.DEGRADED)

    def test_reconnect_resubscribes_real_transport(self) -> None:
        catalog = _nifty_catalog()
        socket = ScriptedSocket([_auth_ok(), _auth_ok()])
        adapter = TrueDataAdapter(
            settings=TrueDataSettings(mode="real", reconnect_policy="bounded_backoff", max_attempts=3, max_backoff_seconds=8),
            environ={"TRUEDATA_USERNAME": "user", "TRUEDATA_PASSWORD": "pass"},
            socket_factory=lambda: socket,
            catalog_loader=lambda: catalog,
            clock=FrozenClock(AS_OF),
        )
        adapter.connect()
        desired = adapter._desired
        self.assertTrue(desired)
        adapter.transport.connected = False
        adapter._state = SessionHealth.DEGRADED
        adapter._on_feed_error("FEED_DISCONNECTED")
        adapter.clock.advance(timedelta(seconds=2))
        adapter.poll()
        self.assertGreaterEqual(adapter.reconnect_count, 1)
        self.assertGreaterEqual(adapter.transport.sent.count("auth"), 2)
        self.assertTrue(any(item.startswith("addsymbol:") for item in socket.sent))
        self.assertTrue(any(item.get("type") == "RESUBSCRIBE" for item in adapter.subscription_events))


class RealPathPaperTests(unittest.TestCase):
    def _protocol_loop(self, extra_ticks: list[str] | None = None):
        catalog = _nifty_catalog()
        ticks = [_trade(catalog["mapping"][0][1], float(catalog["spots"]["NIFTY"]), seq=1)]
        seq = 2
        by_symbol = {
            _td_symbol(row["underlying"], row["expiry"], row["strike"], row["option_type"]): row
            for row in bullish_event(underlyings=("NIFTY",))["contract_master"]
            if row["underlying"] == "NIFTY"
        }
        quotes_by = {
            (q["underlying"], q["expiry"], float(q["strike"]), q["option_type"]): q
            for q in catalog["quotes"]
        }
        for _name, ident in catalog["mapping"][1:]:
            master = by_symbol.get(_name)
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
        if extra_ticks:
            ticks.extend(extra_ticks)
        incoming = [_auth_ok(), '{"success": true, "message": "symbols added", "symbolsadded": ' + str(catalog["mapping"]).replace("'", '"') + "}"] + ticks
        socket = ScriptedSocket(incoming)
        adapter = TrueDataAdapter(
            settings=TrueDataSettings(mode="real", reconnect_policy="fail_closed"),
            environ={"TRUEDATA_USERNAME": "user", "TRUEDATA_PASSWORD": "pass"},
            socket_factory=lambda: socket,
            catalog_loader=lambda: catalog,
            clock=FrozenClock(AS_OF),
        )
        cfg = _live_config(provider="truedata", snapshot_interval_seconds=0, session_timeout_seconds=86400)
        loop = LivePaperLoop(cfg, adapter, clock=FrozenClock(AS_OF), risk_secret=TEST_RISK_SECRET)
        loop.start()
        return loop, catalog, ticks

    def test_protocol_path_opens_paper(self) -> None:
        loop, _catalog, ticks = self._protocol_loop()
        opened = None
        for _ in range(len(ticks) + 3):
            reports = loop.run_once("NIFTY")
            for report in reports:
                if report.status == CycleStatus.PAPER_FILL:
                    opened = report
                    break
            if opened:
                break
        self.assertIsNotNone(opened)
        self.assertEqual(opened.status, CycleStatus.PAPER_FILL, opened.reason if opened else "no fill")
        self.assertEqual(loop.positions.open_positions()[0].lot_size, 75)

    def test_protocol_path_closes_paper(self) -> None:
        catalog = _nifty_catalog()
        loop, _cat, ticks = self._protocol_loop()
        opened = None
        for _ in range(len(ticks) + 3):
            reports = loop.run_once("NIFTY")
            for report in reports:
                if report.status == CycleStatus.PAPER_FILL:
                    opened = report
                    break
            if opened:
                break
        self.assertIsNotNone(opened)
        pos = loop.positions.open_positions()[0]
        stop_ticks = []
        seq = 900
        for _name, ident in catalog["mapping"][1:]:
            if pos.contract_id.split("-")[-1] == "CE" and "CE" not in _name:
                continue
            if pos.contract_id.split("-")[-1] == "PE" and "PE" not in _name:
                continue
            stop_ticks.append(_trade(ident, 10.0, bid=10.0, ask=11.0, seq=seq, oi=5000, volume=200))
            seq += 1
        loop.provider.transport._ws.incoming[:] = stop_ticks
        closed = None
        for _ in range(len(stop_ticks) + 8):
            reports = loop.run_once("NIFTY")
            for report in reports:
                if report.status == CycleStatus.PAPER_CLOSE:
                    closed = report
                    break
            if closed:
                break
        self.assertIsNotNone(closed)
        self.assertEqual(loop.positions.all()[0].state, PositionState.CLOSED)

    def test_heartbeat_is_control_no_trade(self) -> None:
        catalog = _nifty_catalog()
        adapter, _sock = _real_adapter(
            [_auth_ok(), '{"HeartBeat": {"timestamp": "' + AS_OF.isoformat() + '", "message": "heartbeat"}}'],
            catalog,
        )
        cfg = _live_config(provider="truedata", snapshot_interval_seconds=0, session_timeout_seconds=86400)
        loop = LivePaperLoop(cfg, adapter, clock=FrozenClock(AS_OF), risk_secret=TEST_RISK_SECRET)
        loop.start()
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertEqual(report.reason, "FEED_HEARTBEAT")
        self.assertFalse(loop.positions.open_positions())
        self.assertIsNone(loop.last_snapshot)
        self.assertIsNone(loop._last_sequence)
        self.assertIsNotNone(loop.health.last_heartbeat_at)


class CatalogTests(unittest.TestCase):
    def test_parse_symbol_list(self) -> None:
        rows = parse_catalog_text("NIFTY 50\nNIFTY26092225000CE,75\nFINNIFTY26092225000PE,40\n")
        self.assertEqual(rows[0]["canonical_symbol"], "NIFTY")
        self.assertEqual(rows[0]["instrument_type"], "INDEX")
        self.assertEqual(rows[1]["option_type"], "CE")
        self.assertEqual(rows[1]["lot_size"], 75)
        self.assertEqual(rows[1]["expiry"], date(2026, 9, 22))
        self.assertEqual(rows[2]["canonical_symbol"], "FINNIFTY")
        self.assertEqual(rows[1]["expiry_class"], UNKNOWN_EXPIRY_CLASS)
        self.assertNotEqual(rows[1]["expiry_class"], "WEEKLY")
        self.assertIsNone(rows[0]["expiry_class"])
        merged = merge_catalog(rows)
        self.assertEqual(len(merged), 3)

    def test_empty_and_malformed_catalog(self) -> None:
        self.assertEqual(parse_catalog_text(""), [])
        with self.assertRaises(GrowConfigError) as ctx:
            parse_catalog_text("{nope")
        self.assertIn("METADATA_UNAVAILABLE", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            parse_catalog_text(None)  # type: ignore[arg-type]
        self.assertIn("METADATA_UNAVAILABLE", str(ctx.exception))

    def test_json_catalog_keeps_symbol_id(self) -> None:
        rows = parse_catalog_text(
            json.dumps(
                {
                    "instruments": [
                        {
                            "provider_symbol": "NIFTY26092225000CE",
                            "symbol_id": 42,
                            "lot_size": 75,
                            "expiry_class": "WEEKLY",
                        }
                    ]
                }
            )
        )
        self.assertEqual(rows[0]["provider_symbol_id"], "42")
        self.assertEqual(rows[0]["provider_symbol"], "NIFTY26092225000CE")
        self.assertNotEqual(rows[0]["provider_symbol"], "NIFTY-2026-09-22-25000-CE")
        self.assertEqual(rows[0]["expiry_class"], "WEEKLY")


class ExpiryClassTests(unittest.TestCase):
    def test_explicit_weekly_remains_weekly(self) -> None:
        row = normalize_catalog_row(
            {
                "provider_symbol": "NIFTY26092225000CE",
                "lot_size": 75,
                "expiry_class": "WEEKLY",
            }
        )
        self.assertEqual(row["expiry_class"], "WEEKLY")

    def test_explicit_monthly_remains_monthly(self) -> None:
        row = normalize_catalog_row(
            {
                "provider_symbol": "NIFTY26092925000CE",
                "lot_size": 75,
                "expiry_class": "MONTHLY",
            }
        )
        self.assertEqual(row["expiry_class"], "MONTHLY")

    def test_missing_expiry_class_is_unknown_at_parse(self) -> None:
        row = normalize_catalog_row({"provider_symbol": "NIFTY26092225000CE", "lot_size": 75})
        self.assertEqual(row["expiry_class"], UNKNOWN_EXPIRY_CLASS)
        self.assertNotEqual(row["expiry_class"], "WEEKLY")

    def test_calendar_classifies_missing_weekly_and_keeps_unscheduled_unknown(self) -> None:
        weekly = {
            "provider_symbol": "NIFTY26092225000CE",
            "lot_size": 75,
            "expiry": date(2026, 9, 22),
            "option_type": "CE",
        }
        unscheduled = {
            "provider_symbol": "NIFTY26092125000CE",
            "lot_size": 75,
            "expiry": date(2026, 9, 21),
            "option_type": "CE",
        }
        adapter = TrueDataAdapter(
            events=(),
            catalog=[weekly, unscheduled, {"provider_symbol": "NIFTY 50", "lot_size": None}],
            settings=_settings(),
            clock=FrozenClock(AS_OF),
        )
        adapter._spots["NIFTY"] = 25000.0
        adapter.connect()
        by_symbol = {item["provider_symbol"]: item for item in adapter.instrument_catalog()}
        self.assertEqual(by_symbol["NIFTY26092225000CE"]["expiry_class"], "WEEKLY")
        self.assertEqual(by_symbol["NIFTY26092225000CE"]["classification"]["evidence_source"], "CALENDAR")
        self.assertEqual(by_symbol["NIFTY26092125000CE"]["expiry_class"], UNKNOWN_EXPIRY_CLASS)
        self.assertNotEqual(by_symbol["NIFTY26092125000CE"]["expiry_class"], "WEEKLY")
        self.assertTrue(any("NIFTY26092225000CE" in symbol for symbol in adapter._desired))
        self.assertFalse(any("NIFTY26092125000CE" in symbol for symbol in adapter._desired))

    def test_monthly_only_rejects_unknown_and_weekly(self) -> None:
        catalog = [
            {"provider_symbol": "NIFTY 50", "lot_size": None},
            {"provider_symbol": "NIFTY26092225000CE", "lot_size": 75, "expiry": date(2026, 9, 22), "option_type": "CE"},
            {"provider_symbol": "NIFTY26092125000CE", "lot_size": 75, "expiry": date(2026, 9, 21), "option_type": "CE"},
        ]
        policies = tuple(
            replace(policy, expiry_policy_profile=MONTHLY_ONLY) if policy.canonical_symbol == "NIFTY" else policy
            for policy in default_index_policies()
        )
        adapter = TrueDataAdapter(
            events=(),
            catalog=catalog,
            settings=_settings(),
            clock=FrozenClock(AS_OF),
            registry=IndexUniverseRegistry(policies),
        )
        records = adapter._expiry_records("NIFTY")
        by_day = {rec.expiry: rec.expiry_class for rec in records}
        self.assertEqual(by_day[date(2026, 9, 22)], "WEEKLY")
        self.assertEqual(by_day[date(2026, 9, 21)], UNKNOWN_EXPIRY_CLASS)
        resolved = resolve_nearest_expiry("NIFTY", AS_OF, records, MONTHLY_ONLY, allow_same_day=False)
        self.assertIsNone(resolved.selected_expiry)
        self.assertIsNone(resolved.selected_expiry_class)
        self.assertTrue(any(DISALLOWED_CLASS in reason or NO_ELIGIBLE_EXPIRY in reason for reason in resolved.exclusion_reasons))

    def test_weekly_preferred_does_not_treat_unknown_as_weekly(self) -> None:
        catalog = [
            {"provider_symbol": "NIFTY 50", "lot_size": None},
            {
                "provider_symbol": "NIFTY26092125000CE",
                "lot_size": 75,
                "expiry": date(2026, 9, 21),
                "option_type": "CE",
            },
            {
                "provider_symbol": "NIFTY26092425000CE",
                "lot_size": 75,
                "expiry": date(2026, 9, 24),
                "option_type": "CE",
                "expiry_class": "MONTHLY",
            },
        ]
        adapter = TrueDataAdapter(events=(), catalog=catalog, settings=_settings(), clock=FrozenClock(AS_OF))
        adapter._spots["NIFTY"] = 25000.0
        adapter.connect()
        records = adapter._expiry_records("NIFTY")
        by_class = {rec.expiry.isoformat(): rec.expiry_class for rec in records}
        self.assertEqual(by_class["2026-09-21"], UNKNOWN_EXPIRY_CLASS)
        self.assertEqual(by_class["2026-09-24"], "MONTHLY")
        resolved = resolve_nearest_expiry("NIFTY", AS_OF, records, WEEKLY_PREFERRED, allow_same_day=False)
        self.assertIsNone(resolved.selected_expiry)
        self.assertNotEqual(resolved.selected_expiry_class, "WEEKLY")
        self.assertFalse(any("NIFTY26092125000CE" in symbol for symbol in adapter._desired))


class SymbolMapReconnectTests(unittest.TestCase):
    def _index_catalog(self):
        return {
            "instruments": [
                {"provider_symbol": "NIFTY 50", "canonical_symbol": "NIFTY", "instrument_type": "INDEX", "lot_size": None}
            ],
            "spot_bars": [],
            "quotes": [],
            "spots": {"NIFTY": 25040.0},
        }

    def test_tick_before_symbolsadded_is_rejected(self) -> None:
        catalog = self._index_catalog()
        adapter, _sock = _real_adapter([_auth_ok(), _trade(101, 25040.0, seq=1)], catalog)
        adapter.connect()
        self.assertFalse(adapter._mapping_ready)
        self.assertEqual(adapter._symbol_ids, {})
        with self.assertRaises(GrowConfigError) as ctx:
            adapter.poll()
        self.assertIn("SYMBOL_MAP_NOT_READY", str(ctx.exception))

    def test_symbol_map_is_connection_scoped(self) -> None:
        catalog = self._index_catalog()
        incoming = [
            _auth_ok(),
            _symbolsadded([["NIFTY 50", 101]]),
            _trade(101, 25040.0, seq=1),
            _auth_ok(),
            _symbolsadded([["NIFTY 50", 201]]),
            _trade(201, 25041.0, seq=2),
            _trade(101, 25042.0, seq=3),
        ]
        adapter, _sock = _real_adapter(
            incoming,
            catalog,
            reconnect_policy="bounded_backoff",
        )
        adapter.connect()
        ack = adapter.poll()
        self.assertEqual(ack["kind"], "subscribe")
        self.assertEqual(adapter._symbol_ids, {"101": "NIFTY 50"})
        self.assertTrue(adapter._mapping_ready)
        snap = adapter.poll()
        self.assertEqual(snap["spots"]["NIFTY"], 25040.0)
        adapter.transport.connected = False
        adapter._state = SessionHealth.DEGRADED
        adapter._on_feed_error("FEED_DISCONNECTED")
        self.assertFalse(adapter._mapping_ready)
        self.assertEqual(adapter._symbol_ids, {})
        adapter.clock.advance(timedelta(seconds=2))
        ack2 = adapter.poll()
        self.assertEqual(ack2["kind"], "subscribe")
        self.assertEqual(adapter._symbol_ids, {"201": "NIFTY 50"})
        self.assertNotIn("101", adapter._symbol_ids)
        self.assertTrue(adapter._mapping_ready)
        snap2 = adapter.poll()
        self.assertEqual(snap2["spots"]["NIFTY"], 25041.0)
        with self.assertRaises(GrowConfigError) as ctx:
            adapter.poll()
        self.assertIn("UNKNOWN_SYMBOL_ID", str(ctx.exception))

    def test_tick_before_new_symbolsadded_after_reconnect(self) -> None:
        catalog = self._index_catalog()
        incoming = [
            _auth_ok(),
            _symbolsadded([["NIFTY 50", 101]]),
            _trade(101, 25040.0, seq=1),
            _auth_ok(),
            _trade(201, 25041.0, seq=2),
        ]
        adapter, _sock = _real_adapter(incoming, catalog, reconnect_policy="bounded_backoff")
        adapter.connect()
        adapter.poll()
        adapter.poll()
        adapter.transport.connected = False
        adapter._state = SessionHealth.DEGRADED
        adapter._on_feed_error("FEED_DISCONNECTED")
        adapter.clock.advance(timedelta(seconds=2))
        self.assertFalse(adapter._mapping_ready)
        with self.assertRaises(GrowConfigError) as ctx:
            adapter.poll()
        self.assertIn("SYMBOL_MAP_NOT_READY", str(ctx.exception))
        self.assertNotIn("101", adapter._symbol_ids)

    def test_reconnect_respects_max_symbols(self) -> None:
        catalog = _nifty_catalog()
        socket = ScriptedSocket([_auth_ok(), _auth_ok()])
        adapter = TrueDataAdapter(
            settings=TrueDataSettings(
                mode="real",
                reconnect_policy="bounded_backoff",
                max_attempts=3,
                max_backoff_seconds=8,
                max_symbols=3,
            ),
            environ={"TRUEDATA_USERNAME": "user", "TRUEDATA_PASSWORD": "pass"},
            socket_factory=lambda: socket,
            catalog_loader=lambda: catalog,
            clock=FrozenClock(AS_OF),
        )
        adapter._spots["NIFTY"] = float(catalog["spots"]["NIFTY"])
        adapter.connect()
        self.assertLessEqual(len(adapter._desired), 3)
        before = adapter._desired
        adapter.transport.connected = False
        adapter._state = SessionHealth.DEGRADED
        adapter._on_feed_error("FEED_DISCONNECTED")
        adapter.clock.advance(timedelta(seconds=2))
        adapter.poll()
        self.assertLessEqual(len(adapter._desired), 3)
        self.assertEqual(adapter._desired, before)
        addsymbol = [item for item in socket.sent if item.startswith("addsymbol:")]
        self.assertTrue(addsymbol)
        for payload in addsymbol:
            symbols = payload.split(":", 1)[1].split("+") if ":" in payload else []
            self.assertLessEqual(len([s for s in symbols if s]), 3)


class SmokeScriptTests(unittest.TestCase):
    def test_smoke_does_not_inject_catalog_or_broker(self) -> None:
        text = (ROOT / "scripts" / "run_truedata_smoke.py").read_text(encoding="utf-8")
        self.assertNotIn("catalog_loader", text)
        self.assertNotIn("sample_ticks", text)
        self.assertNotIn("place_order", text)
        self.assertNotIn("kiteconnect", text.lower())
        self.assertIn("TRUEDATA_USERNAME", text)
        self.assertIn("build_smoke_report", text)
        self.assertIn("TRUEDATA_SMOKE", text)


