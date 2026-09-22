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
from grow.history.universe import (
    IndexPolicy,
    IndexUniverseRegistry,
    WEEKLY_PREFERRED,
    default_index_policies,
)
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
        expiry_policy_profile=WEEKLY_PREFERRED,
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
