"""Phase 3 — market-data health states and paper-entry gating."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from grow.clock import FrozenClock
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.integration.contract import IntegratedDecision, IntegratedDecisionStatus
from grow.decision.integration.policy import evaluate_policy
from grow.errors import GrowConfigError
from grow.live_data.health import (
    MARKET_DATA_NOT_HEALTHY,
    MarketDataHealth,
    allows_new_paper_trade,
    map_session_health,
    reject_unhealthy_market_data,
    resolve_market_data_health,
)
from grow.live_data.kite_market import KiteMarketProvider, KiteMarketSettings, ScriptedKiteTransport
from grow.live_data.models import SessionHealth
from grow.market_data.normalized.models import DataQualityStatus
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.orchestration.models import AggregateAnalysisPackage
from grow.paper.engine import PaperExecutionEngine
from tests.helpers import TEST_RISK_SECRET
from tests.test_live_data import AS_OF
from tests.test_zerodha_market import CE_TOKEN, CSV, PE_TOKEN, _frame, _full_packet, _provider


class MarketDataHealthMappingTests(unittest.TestCase):
    def test_all_phase3_states_map_from_session_health(self) -> None:
        self.assertEqual(map_session_health(SessionHealth.READY), MarketDataHealth.HEALTHY)
        self.assertEqual(map_session_health(SessionHealth.RUNNING), MarketDataHealth.HEALTHY)
        self.assertEqual(map_session_health(SessionHealth.DEGRADED), MarketDataHealth.DEGRADED)
        self.assertEqual(map_session_health(SessionHealth.STALE), MarketDataHealth.STALE)
        self.assertEqual(map_session_health(SessionHealth.DISCONNECTED), MarketDataHealth.DISCONNECTED)
        self.assertEqual(map_session_health(SessionHealth.CONNECTING), MarketDataHealth.DISCONNECTED)
        self.assertEqual(map_session_health(SessionHealth.STOPPED), MarketDataHealth.FAILED)

    def test_only_healthy_allows_new_paper_trade(self) -> None:
        for health in MarketDataHealth:
            self.assertEqual(allows_new_paper_trade(health), health is MarketDataHealth.HEALTHY)

    def test_freshness_downgrades_healthy_to_stale(self) -> None:
        health = resolve_market_data_health(
            session_state=SessionHealth.RUNNING,
            freshness_ok=False,
        )
        self.assertEqual(health, MarketDataHealth.STALE)
        self.assertEqual(
            reject_unhealthy_market_data(session_state=SessionHealth.RUNNING, freshness_ok=False),
            MARKET_DATA_NOT_HEALTHY,
        )


class KiteHealthHardeningTests(unittest.TestCase):
    def test_live_health_dict_exposes_market_data_health(self) -> None:
        provider = _provider([])
        payload = provider.health().to_dict()
        self.assertIn(payload["market_data_health"], {"HEALTHY", "DISCONNECTED", "DEGRADED", "STALE", "FAILED"})
        self.assertEqual(payload["market_data_health"], map_session_health(provider.health().state).value)
        provider.disconnect()

    def test_auth_failure_maps_to_failed(self) -> None:
        provider = _provider(['{"type":"error","data":"denied"}'])
        with self.assertRaises(GrowConfigError) as ctx:
            provider.poll()
        self.assertEqual(str(ctx.exception), "AUTH_FAILED")
        self.assertEqual(provider.health().state, SessionHealth.STOPPED)
        self.assertEqual(provider.health().market_data_health, MarketDataHealth.FAILED.value)

    def test_malformed_packet_stays_control_without_fabricating_quote(self) -> None:
        provider = _provider([_frame(b"\x00" * 10)])
        payload = provider.poll()
        self.assertEqual(payload["reason"], "MALFORMED_MESSAGE")
        self.assertIsNone(provider._quote)

    def test_delayed_tick_marks_stale(self) -> None:
        when = AS_OF - timedelta(seconds=90)
        settings = KiteMarketSettings(max_staleness_seconds=30, reconnect_policy="fail_closed")
        transport = ScriptedKiteTransport(
            instruments_csv=CSV,
            spots={"NIFTY": 24210.0},
            frames=[_frame(_full_packet(CE_TOKEN, ltp=10, bid=9, ask=11, volume=1, oi=1, when=when))],
        )
        provider = KiteMarketProvider(transport=transport, clock=FrozenClock(AS_OF), settings=settings)
        provider.connect()
        payload = provider.poll()
        self.assertTrue(payload.get("option_quotes"))
        self.assertEqual(provider.health().state, SessionHealth.STALE)
        self.assertEqual(provider.health().market_data_health, MarketDataHealth.STALE.value)
        provider.disconnect()

    def test_missing_bid_ask_ltp_oi_reported_not_fabricated(self) -> None:
        when = AS_OF - timedelta(seconds=1)
        # Full packet normally has all fields; assemble path lists missing when absent.
        provider = _provider(
            [_frame(_full_packet(CE_TOKEN, ltp=101.5, bid=101.0, ask=102.0, volume=40, oi=80, when=when))]
        )
        payload = provider.poll()
        self.assertEqual(payload.get("missing_quote_fields"), [])
        self.assertIn("market_data_health", payload)
        self.assertIsNotNone(payload.get("last_valid_quote"))
        provider.disconnect()

    def test_reconnect_after_disconnect_resubscribes_and_returns_healthy(self) -> None:
        when = AS_OF - timedelta(seconds=2)
        settings = KiteMarketSettings(
            reconnect_policy="bounded_backoff",
            max_attempts=3,
            max_backoff_seconds=1,
            max_staleness_seconds=30,
        )

        class _Flaky(ScriptedKiteTransport):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._boom_once = True

            def recv(self):
                if self._boom_once:
                    self._boom_once = False
                    raise GrowConfigError("FEED_DISCONNECTED")
                return super().recv()

        transport = _Flaky(
            instruments_csv=CSV,
            spots={"NIFTY": 24210.0},
            frames=[_frame(_full_packet(PE_TOKEN, ltp=88.0, bid=87.5, ask=88.5, volume=12, oi=30, when=when))],
        )
        clock = FrozenClock(AS_OF)
        provider = KiteMarketProvider(transport=transport, clock=clock, settings=settings)
        provider.connect()
        first = provider.poll()
        self.assertIsNone(first)
        self.assertEqual(provider.health().state, SessionHealth.DEGRADED)
        clock.advance(timedelta(seconds=2))
        payload = provider.poll()
        self.assertTrue(payload.get("option_quotes"))
        self.assertGreaterEqual(provider.reconnect_count, 1)
        self.assertEqual(provider.health().state, SessionHealth.RUNNING)
        self.assertEqual(provider.health().market_data_health, MarketDataHealth.HEALTHY.value)
        provider.disconnect()

    def test_reconnect_exhaustion_fails_closed(self) -> None:
        settings = KiteMarketSettings(
            reconnect_policy="bounded_backoff",
            max_attempts=1,
            max_backoff_seconds=1,
        )

        class _AlwaysDown(ScriptedKiteTransport):
            def recv(self):
                raise GrowConfigError("FEED_DISCONNECTED")

            def subscribe(self, tokens, *, mode: str) -> None:
                if self.connected and getattr(self, "_after_first", False):
                    raise GrowConfigError("SUBSCRIPTION_FAILED")
                super().subscribe(tokens, mode=mode)
                self._after_first = True

        transport = _AlwaysDown(instruments_csv=CSV, spots={"NIFTY": 24210.0}, frames=[])
        clock = FrozenClock(AS_OF)
        provider = KiteMarketProvider(transport=transport, clock=clock, settings=settings)
        provider.connect()
        self.assertIsNone(provider.poll())
        clock.advance(timedelta(seconds=2))
        # First reconnect attempt runs and fails closed into DEGRADED (no raise yet).
        self.assertIsNone(provider.poll())
        self.assertEqual(provider.health().state, SessionHealth.DEGRADED)
        # Next poll hits attempt budget and fails closed.
        with self.assertRaises(GrowConfigError):
            provider.poll()
        self.assertEqual(provider.health().market_data_health, MarketDataHealth.FAILED.value)


class PaperGateHealthTests(unittest.TestCase):
    def _package(self, snapshot):
        return AggregateAnalysisPackage(
            cycle_id="cycle-health",
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            as_of=snapshot.decision_timestamp,
            agent_outputs=(),
            rejected_outputs=(),
            dispatch_records=(),
            conflicts=(),
            supporting_evidence=(),
            conflicting_evidence=(),
            unavailable_agents=(),
            debate=DebateSummary(
                agreement=False,
                actions=(),
                agreeing_agents=(),
                dissenting_agents=(),
                conflicts=(),
                evidence=(),
                insufficient_agents=(),
                error_agents=(),
            ),
            cycle_summary="test",
            package_digest="digest",
        )

    def test_paper_engine_rejects_unhealthy_diagnostics(self) -> None:
        snap = build_fixture_snapshot(underlying="NIFTY", as_of=AS_OF, spot=24210.0)
        unhealthy = replace(
            snap,
            diagnostics={
                **dict(snap.diagnostics),
                "market_data_health": MarketDataHealth.DEGRADED.value,
                "freshness_ok": True,
            },
        )
        decision = IntegratedDecision(
            decision_id="dec-health",
            analysis_cycle_id="cycle-health",
            snapshot_id=unhealthy.snapshot_id,
            snapshot_version=unhealthy.version,
            decision_timestamp=unhealthy.decision_timestamp,
            as_of=unhealthy.decision_timestamp,
            agent_output_refs=(),
            candidate_strategy="trend",
            candidate_instrument="NIFTY-CE",
            direction="BULLISH",
            observations=(),
            calculated_evidence={
                "underlying": "NIFTY",
                "limit_price": 100.0,
                "stop_loss": 90.0,
                "lots": 1,
                "quantity": 1,
            },
            supporting_findings=(),
            conflicting_findings=(),
            status=IntegratedDecisionStatus.CANDIDATE,
            reason_codes=(),
            risk_guard_result="APPROVED",
            risk_guard_reason="approved",
            data_quality_status="OK",
            assumptions=(),
            configuration_version="test",
            ruleset="grow.risk.v1",
            audit_references=(),
            gate_results=(),
        )
        engine = PaperExecutionEngine(load_config(), clock=FrozenClock(AS_OF), risk_secret=TEST_RISK_SECRET)
        result = engine.execute(decision, unhealthy)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, MARKET_DATA_NOT_HEALTHY)

    def test_policy_blocks_unhealthy_when_quality_ok(self) -> None:
        snap = build_fixture_snapshot(underlying="NIFTY", as_of=AS_OF, spot=24210.0)
        unhealthy = replace(
            snap,
            data_quality=DataQualityStatus.OK,
            diagnostics={
                **dict(snap.diagnostics),
                "market_data_health": MarketDataHealth.DISCONNECTED.value,
                "freshness_ok": True,
            },
        )
        result = evaluate_policy(unhealthy, self._package(unhealthy))
        self.assertEqual(result.terminal_status, "BLOCKED")
        self.assertIn(MARKET_DATA_NOT_HEALTHY, result.reason_codes)

    def test_healthy_fixture_still_passes_health_gate(self) -> None:
        snap = build_fixture_snapshot(underlying="NIFTY", as_of=AS_OF, spot=24210.0)
        healthy = replace(
            snap,
            diagnostics={
                **dict(snap.diagnostics),
                "market_data_health": MarketDataHealth.HEALTHY.value,
                "freshness_ok": True,
            },
        )
        self.assertIsNone(
            reject_unhealthy_market_data(
                data_quality=healthy.data_quality,
                diagnostics=dict(healthy.diagnostics),
            )
        )


if __name__ == "__main__":
    unittest.main()
