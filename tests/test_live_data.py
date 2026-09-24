from __future__ import annotations

import ast
import pathlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.errors import GrowConfigError, GrowLiveTradingDisabled
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.live_data.loop import LivePaperLoop, open_loop
from grow.live_data.mock import MockStreamProvider, bearish_event, bullish_event, stream_event
from grow.live_data.models import MOCK_PROVIDER_ID, CycleStatus, SessionHealth
from grow.live_data.normalize import normalize_event
from grow.live_data.provider import open_provider
from grow.market.session import SessionCalendar
from grow.data.schema import Timeframe
from grow.options.models import DecisionStatus
from grow.strategies.signal import StrategySignal
from tests.helpers import TEST_RISK_SECRET, research_fixture_config


AS_OF = datetime(2026, 9, 18, 11, 0, tzinfo=IST)


def _live_config(**changes):
    # LivePaperLoop fixtures use research-sized capital, not the ₹10K operator profile.
    base = research_fixture_config()
    live = replace(base.live_data, enabled=True, **changes) if changes else replace(base.live_data, enabled=True)
    cfg = replace(base, live_data=live)
    cfg.assert_safe()
    return cfg


def _loop(events, config=None) -> LivePaperLoop:
    cfg = config or _live_config()
    loop = LivePaperLoop(
        cfg,
        MockStreamProvider(events=tuple(events)),
        clock=FrozenClock(AS_OF),
        risk_secret=TEST_RISK_SECRET,
    )
    loop.start()
    return loop


def _stub_signal(market, direction: str = "BULLISH") -> StrategySignal:
    return StrategySignal(
        symbol=market.symbol,
        strategy="ema_trend",
        direction=direction,
        entry=100.0,
        stop=90.0,
        target=120.0,
        confidence=0.6,
        timeframe=Timeframe.M15,
        reason="stub",
        as_of=market.as_of,
        snapshot_id=market.snapshot_id,
        signal_id="sig-3a",
        strategy_version="v1",
    )


class ProviderTests(unittest.TestCase):
    def test_open_provider_is_explicit(self) -> None:
        self.assertEqual(open_provider("mock").identity, MOCK_PROVIDER_ID)
        for bad, code in (
            ("fixture", "FIXTURE_FALLBACK_FORBIDDEN"),
            ("historical", "FIXTURE_FALLBACK_FORBIDDEN"),
            ("grow.data.fixture.v1", "FIXTURE_FALLBACK_FORBIDDEN"),
            ("live.nse", "PROVIDER_NOT_APPROVED"),
            ("kite", "PROVIDER_NOT_APPROVED"),
        ):
            with self.assertRaises(GrowConfigError) as ctx:
                open_provider(bad)
            self.assertIn(code, str(ctx.exception))

    def test_fixture_payload_rejected(self) -> None:
        provider = MockStreamProvider(events=({"provider": "fixture", "sequence": 1, "is_fixture": True},))
        provider.connect()
        with self.assertRaises(GrowConfigError) as ctx:
            provider.poll()
        self.assertIn("FIXTURE_FALLBACK_FORBIDDEN", str(ctx.exception))

    def test_open_loop_uses_mock_only(self) -> None:
        loop = open_loop(_live_config(), clock=FrozenClock(AS_OF), risk_secret=TEST_RISK_SECRET, events=(bullish_event(),))
        self.assertEqual(loop.provider.identity, MOCK_PROVIDER_ID)
        self.assertNotIn("live", loop.provider.identity.lower())


class NormalizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = _live_config()
        self.cal = SessionCalendar(self.cfg.market, clock=FrozenClock(AS_OF))

    def test_valid_snapshot(self) -> None:
        snap = normalize_event(bullish_event(), now=AS_OF, max_staleness_seconds=30, calendar=self.cal)
        self.assertEqual(snap.provider_id, MOCK_PROVIDER_ID)
        self.assertTrue(snap.freshness_ok)
        self.assertIn("NIFTY", snap.underlyings)
        self.assertIn("BANKNIFTY", snap.underlyings)
        self.assertIn("MIDCPNIFTY", snap.underlyings)
        self.assertFalse(snap.market["NIFTY"].source.is_fixture)
        self.assertTrue(snap.market["NIFTY"].source.is_live)
        self.assertGreaterEqual(len(snap.market["NIFTY"].series[Timeframe.M15].bars), 50)
        chain = snap.chains["NIFTY"]
        self.assertFalse(chain.is_fixture)
        self.assertEqual(chain.source_id, MOCK_PROVIDER_ID)
        self.assertNotIn("live", chain.source_id.lower())
        self.assertGreater(len(chain.expiries), 1)
        nifty = next(c for c in chain.contracts if c.option_type.value == "CE")
        canonical = f"{nifty.underlying}-{nifty.expiry.isoformat()}-{int(nifty.strike)}-CE"
        self.assertNotEqual(nifty.provider_contract_id, canonical)
        self.assertEqual(snap.lot_sizes[nifty.provider_contract_id], 75)
        self.assertEqual(snap.lot_sizes[canonical], 75)

    def test_naive_timestamp_rejected(self) -> None:
        with self.assertRaises(GrowConfigError) as ctx:
            normalize_event(bullish_event(naive=True), now=AS_OF, max_staleness_seconds=30, calendar=self.cal)
        self.assertIn("NAIVE_TIMESTAMP", str(ctx.exception))

    def test_missing_event_time_rejected(self) -> None:
        payload = bullish_event()
        payload.pop("event_time")
        with self.assertRaises(GrowConfigError) as ctx:
            normalize_event(payload, now=AS_OF, max_staleness_seconds=30, calendar=self.cal)
        self.assertIn("MISSING_TIMESTAMP", str(ctx.exception))

    def test_invalid_timestamp_rejected(self) -> None:
        payload = bullish_event()
        payload["event_time"] = "not-a-timestamp"
        with self.assertRaises(GrowConfigError) as ctx:
            normalize_event(payload, now=AS_OF, max_staleness_seconds=30, calendar=self.cal)
        self.assertIn("INVALID_TIMESTAMP", str(ctx.exception))

    def test_stale_snapshot(self) -> None:
        later = AS_OF + timedelta(minutes=5)
        snap = normalize_event(
            bullish_event(),
            now=later,
            max_staleness_seconds=30,
            calendar=self.cal,
        )
        self.assertFalse(snap.freshness_ok)
        self.assertTrue(any(item.startswith("STALE") for item in snap.diagnostics))

    def test_future_event_time_rejected(self) -> None:
        payload = bullish_event()
        future = AS_OF + timedelta(seconds=5)
        payload["event_time"] = future.isoformat()
        payload["received_time"] = future.isoformat()
        with self.assertRaises(GrowConfigError) as ctx:
            normalize_event(payload, now=AS_OF, max_staleness_seconds=30, calendar=self.cal)
        self.assertIn("FUTURE_SNAPSHOT", str(ctx.exception))

    def test_future_received_time_rejected(self) -> None:
        payload = bullish_event()
        payload["received_time"] = (AS_OF + timedelta(seconds=5)).isoformat()
        with self.assertRaises(GrowConfigError) as ctx:
            normalize_event(payload, now=AS_OF, max_staleness_seconds=30, calendar=self.cal)
        self.assertIn("FUTURE_RECEIVED_TIME", str(ctx.exception))

    def test_normal_event_time_accepted(self) -> None:
        snap = normalize_event(bullish_event(), now=AS_OF, max_staleness_seconds=30, calendar=self.cal)
        self.assertTrue(snap.freshness_ok)
        self.assertEqual(snap.event_time, AS_OF)
        self.assertFalse(any("FUTURE" in item or item.startswith("STALE") for item in snap.diagnostics))

    def test_future_timestamp_is_not_treated_as_fresh(self) -> None:
        payload = bullish_event()
        future = AS_OF + timedelta(seconds=1)
        payload["event_time"] = future.isoformat()
        payload["received_time"] = future.isoformat()
        with self.assertRaises(GrowConfigError) as ctx:
            normalize_event(payload, now=AS_OF, max_staleness_seconds=30, calendar=self.cal)
        self.assertEqual(str(ctx.exception), "FUTURE_SNAPSHOT")

    def test_out_of_order_and_duplicate(self) -> None:
        first = normalize_event(bullish_event(sequence=2), now=AS_OF, max_staleness_seconds=30, calendar=self.cal)
        with self.assertRaises(GrowConfigError) as ctx:
            normalize_event(
                bullish_event(sequence=1),
                now=AS_OF,
                max_staleness_seconds=30,
                calendar=self.cal,
                last_sequence=first.sequence,
            )
        self.assertIn("OUT_OF_ORDER", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            normalize_event(
                bullish_event(sequence=2),
                now=AS_OF,
                max_staleness_seconds=30,
                calendar=self.cal,
                last_sequence=2,
            )
        self.assertIn("DUPLICATE_SEQUENCE", str(ctx.exception))

    def test_invalid_option_type(self) -> None:
        payload = bullish_event()
        payload["contract_master"][0]["option_type"] = "XX"
        with self.assertRaises(GrowConfigError) as ctx:
            normalize_event(payload, now=AS_OF, max_staleness_seconds=30, calendar=self.cal)
        self.assertIn("INVALID_OPTION_TYPE", str(ctx.exception))

    def test_malformed_expiry_rejected(self) -> None:
        payload = bullish_event()
        payload["contract_master"][0]["expiry"] = "32-13-99"
        with self.assertRaises(GrowConfigError) as ctx:
            normalize_event(payload, now=AS_OF, max_staleness_seconds=30, calendar=self.cal)
        self.assertIn("INVALID_EXPIRY", str(ctx.exception))

    def test_stream_event_helper_is_not_nifty_only(self) -> None:
        payload = stream_event(underlyings=("NIFTY", "MIDCPNIFTY"))
        snap = normalize_event(payload, now=AS_OF, max_staleness_seconds=30, calendar=self.cal)
        self.assertEqual(snap.underlyings, ("NIFTY", "MIDCPNIFTY"))


class LoopTests(unittest.TestCase):
    def test_bullish_ce_paper_fill(self) -> None:
        loop = _loop([bullish_event()])
        reports = loop.run_once("NIFTY")
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report.status, CycleStatus.PAPER_FILL, report.reason)
        self.assertEqual(report.option_type, "CE")
        self.assertEqual(report.lot_size, 75)
        self.assertEqual(report.lots, 1)
        self.assertIsNotNone(report.fill)
        self.assertEqual(report.fill.quantity, 75)
        self.assertEqual(len(loop.ledger.book.fills), 1)
        self.assertTrue(report.verdict.approved)
        self.assertEqual(loop.health.state, SessionHealth.RUNNING)
        dumped = report.to_dict()
        self.assertTrue(dumped["paper_only"])
        self.assertFalse(dumped["live_trading"])
        self.assertEqual(dumped["decision_id"], report.decision_id)
        self.assertEqual(dumped["candidate_id"], report.candidate_id)

    def test_bearish_pe_paper_fill(self) -> None:
        loop = _loop([bearish_event()])
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.PAPER_FILL, report.reason)
        self.assertEqual(report.option_type, "PE")

    def test_stale_is_no_trade(self) -> None:
        cfg = _live_config(max_staleness_seconds=30)
        loop = LivePaperLoop(
            cfg,
            MockStreamProvider(events=(bullish_event(),)),
            clock=FrozenClock(AS_OF + timedelta(minutes=5)),
            risk_secret=TEST_RISK_SECRET,
        )
        loop.start()
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertTrue(report.reason.startswith("STALE"))
        self.assertEqual(loop.ledger.book.fills, [])
        self.assertEqual(loop.health.state, SessionHealth.STALE)

    def test_future_snapshot_is_no_trade(self) -> None:
        payload = bullish_event()
        future = AS_OF + timedelta(seconds=5)
        payload["event_time"] = future.isoformat()
        payload["received_time"] = future.isoformat()
        loop = _loop([payload])
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertIn("FUTURE_SNAPSHOT", report.reason)
        self.assertEqual(loop.ledger.book.fills, [])
        self.assertNotEqual(loop.health.state, SessionHealth.RUNNING)

    def test_missing_option_chain(self) -> None:
        payload = bullish_event()
        payload["contract_master"] = []
        payload["option_quotes"] = []
        report = _loop([payload]).run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertEqual(report.reason, "MISSING_OPTION_CHAIN")

    def test_missing_ask_is_no_trade(self) -> None:
        payload = bullish_event()
        for quote in payload["option_quotes"]:
            quote["ask"] = None
        loop = _loop([payload])
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertEqual(loop.ledger.book.fills, [])
        self.assertTrue(
            any(token in report.reason for token in ("NO_SPREAD", "NO_LIQUID", "NO_PREMIUM", "UNKNOWN_QUOTE")),
            report.reason,
        )

    def test_missing_lot_size(self) -> None:
        report = _loop([bullish_event(missing_lot=True)]).run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertEqual(report.reason, "MISSING_LOT_SIZE")
        self.assertEqual(report.candidate_id is not None or report.reason == "MISSING_LOT_SIZE", True)

    def test_duplicate_open_is_no_trade(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), bullish_event(sequence=2)],
            config=_live_config(snapshot_interval_seconds=0),
        )
        first = loop.run_once("NIFTY")[0]
        self.assertEqual(first.status, CycleStatus.PAPER_FILL, first.reason)
        second = loop.run_once("NIFTY")[0]
        self.assertEqual(second.status, CycleStatus.NO_TRADE)
        self.assertEqual(second.reason, "DUPLICATE_OPEN_POSITION")
        self.assertEqual(len(loop.ledger.book.fills), 1)

    def test_out_of_order_no_trade(self) -> None:
        loop = _loop(
            [bullish_event(sequence=2), bullish_event(sequence=1)],
            config=_live_config(snapshot_interval_seconds=0),
        )
        first = loop.run_once("NIFTY")[0]
        self.assertEqual(first.status, CycleStatus.PAPER_FILL, first.reason)
        second = loop.run_once("NIFTY")[0]
        self.assertEqual(second.status, CycleStatus.NO_TRADE)
        self.assertIn("OUT_OF_ORDER", second.reason)
        self.assertEqual(len(loop.ledger.book.fills), 1)

    def test_session_timeout_stops_session(self) -> None:
        loop = _loop(
            [bullish_event()],
            config=_live_config(session_timeout_seconds=30, snapshot_interval_seconds=0),
        )
        self.assertIsInstance(loop.clock, FrozenClock)
        loop.clock.advance(timedelta(seconds=30))
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertEqual(report.reason, "SESSION_TIMEOUT")
        self.assertEqual(loop.health.state, SessionHealth.STOPPED)
        self.assertEqual(loop.ledger.book.fills, [])
        later = loop.run_once("NIFTY")[0]
        self.assertEqual(later.status, CycleStatus.NO_TRADE)
        self.assertIn("STOPPED", later.reason)

    def test_session_within_timeout_still_runs(self) -> None:
        loop = _loop(
            [bullish_event()],
            config=_live_config(session_timeout_seconds=30, snapshot_interval_seconds=0, max_staleness_seconds=30),
        )
        loop.clock.advance(timedelta(seconds=10))
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.PAPER_FILL, report.reason)
        self.assertEqual(loop.health.state, SessionHealth.RUNNING)

    def test_snapshot_interval_blocks_until_elapsed(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), bullish_event(sequence=2)],
            config=_live_config(
                snapshot_interval_seconds=10,
                session_timeout_seconds=3600,
                max_staleness_seconds=30,
            ),
        )
        first = loop.run_once("NIFTY")[0]
        self.assertEqual(first.status, CycleStatus.PAPER_FILL, first.reason)
        blocked = loop.run_once("NIFTY")[0]
        self.assertEqual(blocked.status, CycleStatus.NO_TRADE)
        self.assertEqual(blocked.reason, "SNAPSHOT_INTERVAL")
        self.assertEqual(len(loop.ledger.book.fills), 1)
        self.assertEqual(loop.health.state, SessionHealth.RUNNING)
        loop.clock.advance(timedelta(seconds=10))
        nxt = loop.run_once("NIFTY")[0]
        self.assertEqual(nxt.status, CycleStatus.NO_TRADE)
        self.assertEqual(nxt.reason, "DUPLICATE_OPEN_POSITION")
        self.assertEqual(len(loop.ledger.book.fills), 1)

    def test_snapshot_interval_zero_allows_consecutive_cycles(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), bullish_event(sequence=2)],
            config=_live_config(snapshot_interval_seconds=0, session_timeout_seconds=3600),
        )
        first = loop.run_once("NIFTY")[0]
        second = loop.run_once("NIFTY")[0]
        self.assertEqual(first.status, CycleStatus.PAPER_FILL, first.reason)
        self.assertNotEqual(second.reason, "SNAPSHOT_INTERVAL")
        self.assertEqual(second.reason, "DUPLICATE_OPEN_POSITION")

    def test_provider_failure_does_not_consume_interval(self) -> None:
        loop = _loop(
            [
                {"provider": "grow.data.fixture.v1", "sequence": 1, "is_fixture": True},
                bullish_event(sequence=1),
            ],
            config=_live_config(
                snapshot_interval_seconds=10,
                session_timeout_seconds=3600,
                max_staleness_seconds=30,
            ),
        )
        first = loop.run_once("NIFTY")[0]
        self.assertEqual(first.status, CycleStatus.NO_TRADE)
        self.assertIn("FIXTURE_FALLBACK_FORBIDDEN", first.reason)
        self.assertIsNone(loop._last_cycle_at)
        second = loop.run_once("NIFTY")[0]
        self.assertNotEqual(second.reason, "SNAPSHOT_INTERVAL")
        self.assertEqual(second.status, CycleStatus.PAPER_FILL, second.reason)
        self.assertEqual(loop._last_cycle_at, loop.clock.now())

    def test_invalid_snapshot_does_not_consume_interval(self) -> None:
        future = AS_OF + timedelta(seconds=5)
        bad = bullish_event(sequence=1)
        bad["event_time"] = future.isoformat()
        bad["received_time"] = future.isoformat()
        loop = _loop(
            [bad, bullish_event(sequence=1)],
            config=_live_config(
                snapshot_interval_seconds=10,
                session_timeout_seconds=3600,
                max_staleness_seconds=30,
            ),
        )
        first = loop.run_once("NIFTY")[0]
        self.assertEqual(first.status, CycleStatus.NO_TRADE)
        self.assertIn("FUTURE_SNAPSHOT", first.reason)
        self.assertIsNone(loop._last_cycle_at)
        second = loop.run_once("NIFTY")[0]
        self.assertNotEqual(second.reason, "SNAPSHOT_INTERVAL")
        self.assertEqual(second.status, CycleStatus.PAPER_FILL, second.reason)

    def test_interval_measured_from_successful_cycle_completion(self) -> None:
        loop = _loop(
            [bullish_event(sequence=1), bullish_event(sequence=2)],
            config=_live_config(
                snapshot_interval_seconds=10,
                session_timeout_seconds=3600,
                max_staleness_seconds=30,
            ),
        )
        first = loop.run_once("NIFTY")[0]
        self.assertEqual(first.status, CycleStatus.PAPER_FILL, first.reason)
        completed_at = loop.clock.now()
        self.assertEqual(loop._last_cycle_at, completed_at)
        loop.clock.advance(timedelta(seconds=9))
        blocked = loop.run_once("NIFTY")[0]
        self.assertEqual(blocked.reason, "SNAPSHOT_INTERVAL")
        self.assertEqual(loop._last_cycle_at, completed_at)
        loop.clock.advance(timedelta(seconds=1))
        nxt = loop.run_once("NIFTY")[0]
        self.assertNotEqual(nxt.reason, "SNAPSHOT_INTERVAL")
        self.assertEqual(loop._last_cycle_at, loop.clock.now())


    def test_disconnect_no_trade(self) -> None:
        loop = _loop([bullish_event()])
        loop.stop()
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertIn("STOPPED", report.reason)
        self.assertEqual(loop.ledger.book.fills, [])

    def test_risk_guard_rejection(self) -> None:
        base = _live_config()
        cfg = replace(base, risk=replace(base.risk, max_position_notional=1.0))
        loop = _loop([bullish_event()], config=cfg)
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertTrue(report.reason.startswith("RISK_GUARD"), report.reason)
        self.assertEqual(loop.ledger.book.fills, [])

    def test_session_lifecycle_is_auditable(self) -> None:
        loop = _loop([bullish_event()])
        loop.run_once("NIFTY")
        loop.stop()
        states = [dst for _, _, dst in loop.session.transitions]
        self.assertIn("CONNECTING", states)
        self.assertIn("READY", states)
        self.assertIn("RUNNING", states)
        self.assertIn("STOPPED", states)
        dumped = loop.session.to_dict()
        self.assertFalse(dumped["live_trading"])
        self.assertEqual(dumped["provider_id"], MOCK_PROVIDER_ID)

    def test_discovery_is_not_nifty_only(self) -> None:
        loop = _loop([bullish_event()])
        loop.run_once("NIFTY")
        snap = loop.last_snapshot
        self.assertGreaterEqual(len(snap.underlyings), 2)
        self.assertIn("MIDCPNIFTY", snap.underlyings)
        self.assertGreater(len(snap.chains["NIFTY"].expiries), 1)

    def test_paper_stream_accepts_midcpnifty_chain(self) -> None:
        loop = _loop([bullish_event()])
        loop.run_once("NIFTY")
        snap = loop.last_snapshot
        market = snap.market["MIDCPNIFTY"]
        chain = snap.chains["MIDCPNIFTY"]
        decision = loop.options.evaluate(_stub_signal(market, "BULLISH"), market, chain)
        self.assertEqual(decision.status, DecisionStatus.CANDIDATE, decision.diagnostics)
        self.assertEqual(decision.candidate.underlying, "MIDCPNIFTY")
        self.assertEqual(decision.candidate.option_type, "CE")
        self.assertEqual(decision.candidate.intent, "BUY")

    def test_paper_stream_rejects_fixture_chain(self) -> None:
        loop = _loop([bullish_event()])
        loop.run_once("NIFTY")
        snap = loop.last_snapshot
        chain = replace(snap.chains["NIFTY"], is_fixture=True)
        market = snap.market["NIFTY"]
        decision = loop.options.evaluate(_stub_signal(market), market, chain)
        self.assertEqual(decision.status, DecisionStatus.NO_TRADE)
        self.assertIn("FIXTURE_FALLBACK_FORBIDDEN", decision.diagnostics)

    def test_no_fixture_fallback_in_loop(self) -> None:
        loop = _loop([{"provider": "grow.data.fixture.v1", "sequence": 1, "is_fixture": True}])
        report = loop.run_once("NIFTY")[0]
        self.assertEqual(report.status, CycleStatus.NO_TRADE)
        self.assertIn("FIXTURE_FALLBACK_FORBIDDEN", report.reason)


class SafetyTests(unittest.TestCase):
    def test_live_trading_config_rejected(self) -> None:
        base = load_config()
        with self.assertRaises(GrowLiveTradingDisabled):
            replace(base, live_data=replace(base.live_data, live_trading=True)).assert_safe()
        with self.assertRaises(GrowConfigError):
            replace(base, live_data=replace(base.live_data, paper_mode=False)).assert_safe()
        with self.assertRaises(GrowConfigError):
            replace(base, live_data=replace(base.live_data, enabled=True, provider="nse")).assert_safe()
        enabled = replace(base, live_data=replace(base.live_data, enabled=True, paper_mode=True, live_trading=False))
        enabled.assert_safe()
        self.assertFalse(enabled.data.allow_live_feed)
        self.assertFalse(enabled.options.allow_live_chain)

    def test_env_cannot_enable_live_trading_or_vendor(self) -> None:
        with self.assertRaises(GrowLiveTradingDisabled):
            load_config(environ={"GROW_LIVE_DATA_LIVE_TRADING": "true"})
        with self.assertRaises(GrowConfigError):
            load_config(environ={"GROW_LIVE_DATA_PROVIDER": "kite"})
        cfg = load_config(environ={"GROW_LIVE_DATA_ENABLED": "true"})
        self.assertTrue(cfg.live_data.enabled)
        self.assertTrue(cfg.live_data.paper_mode)
        self.assertFalse(cfg.live_data.live_trading)
        self.assertFalse(cfg.data.allow_live_feed)
        self.assertFalse(cfg.options.allow_live_chain)
        self.assertEqual(cfg.options.provider, "fixture")

    def test_compiled_lock_and_no_broker_imports(self) -> None:
        self.assertFalse(LIVE_TRADING_COMPILED)
        root = pathlib.Path(__file__).resolve().parents[1] / "grow" / "live_data"
        banned = {"urllib", "requests", "httpx", "aiohttp", "websocket", "kiteconnect", "upstox", "dhanhq", "zerodha"}
        forbidden_modules = {"grow.execution.live"}
        forbidden_names = {"LiveBroker", "place_live_order", "place_order"}
        for path in root.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".", 1)[0] for alias in node.names]
                    for alias in node.names:
                        self.assertNotIn(alias.name, forbidden_modules, msg=f"{path.name} imports {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".", 1)[0]]
                    self.assertNotIn(node.module, forbidden_modules, msg=f"{path.name} imports {node.module}")
                    for alias in node.names:
                        self.assertNotIn(alias.name, forbidden_names, msg=f"{path.name} imports {alias.name}")
                for name in names:
                    self.assertNotIn(name, banned, msg=f"{path.name} imports {name}")
            for needle in ("place_live_order", "LiveBroker", "kiteconnect"):
                self.assertNotIn(needle, source)
        import grow.live_data.loop as loop_mod

        imported = {getattr(value, "__name__", "") for value in vars(loop_mod).values()}
        self.assertNotIn("grow.execution.live", imported)
        self.assertNotIn("LiveBroker", vars(loop_mod))

    def test_default_config_keeps_live_data_off(self) -> None:
        cfg = load_config()
        self.assertFalse(cfg.live_data.enabled)
        self.assertTrue(cfg.live_data.paper_mode)
        self.assertFalse(cfg.live_data.live_trading)
        self.assertEqual(cfg.live_data.provider, "mock")
        self.assertEqual(cfg.options.provider, "fixture")
        self.assertFalse(cfg.execution.live_trading_enabled)

    def test_timing_config_bounds(self) -> None:
        base = load_config()
        # 0 disables LivePaperLoop-style wall-clock timeout (campaign path).
        replace(base, live_data=replace(base.live_data, session_timeout_seconds=0)).assert_safe()
        with self.assertRaises(GrowConfigError) as ctx:
            replace(base, live_data=replace(base.live_data, session_timeout_seconds=-1)).assert_safe()
        self.assertIn("session_timeout_seconds", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            replace(base, live_data=replace(base.live_data, snapshot_interval_seconds=-1)).assert_safe()
        self.assertIn("snapshot_interval_seconds", str(ctx.exception))
        replace(base, live_data=replace(base.live_data, snapshot_interval_seconds=0)).assert_safe()


if __name__ == "__main__":
    unittest.main()
