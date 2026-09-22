"""Phase 2 — LIVE / FIXTURE / MIXED market-data provenance."""

from __future__ import annotations

import unittest
from dataclasses import replace

from grow.clock import FrozenClock
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.integration.contract import IntegratedDecision, IntegratedDecisionStatus
from grow.decision.integration.policy import evaluate_policy
from grow.errors import GrowConfigError
from grow.live_data.normalize import normalize_event
from grow.market.session import SessionCalendar
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.provenance import (
    MIXED_MARKET_DATA_SOURCE,
    MarketDataSource,
    classify_agent_snapshot,
    classify_fixture_flags,
    reject_mixed_market_data,
)
from grow.market_data.snapshots.builder import build_agent_snapshot, build_fixture_snapshot
from grow.orchestration.models import AggregateAnalysisPackage
from grow.paper.engine import PaperExecutionEngine
from tests.helpers import TEST_RISK_SECRET
from tests.test_live_data import AS_OF, _live_config


def _option(*, option_type: str, is_fixture: bool, strike: float = 24200.0) -> OptionQuoteView:
    return OptionQuoteView(
        underlying="NIFTY",
        expiry=AS_OF.date(),
        strike=strike,
        option_type=option_type,
        ltp=100.0,
        bid=99.0,
        ask=101.0,
        open_interest=50,
        volume=10,
        quote_timestamp=AS_OF,
        quote_age_seconds=0.0,
        provider_contract_id=f"NIFTY-{option_type}-{'FIX' if is_fixture else 'LIVE'}",
        quality=DataQualityStatus.OK,
        lot_size=75,
        is_fixture=is_fixture,
    )


def _kite_payload(*, quotes: list[dict], lot_size: int | None = 75, sequence: int = 1) -> dict:
    master = [
        {
            "tradingsymbol": "NIFTY26SEP24200CE",
            "underlying": "NIFTY",
            "expiry": "2026-09-22",
            "strike": 24200,
            "option_type": "CE",
            "instrument_type": "OPTIDX",
            "expiry_class": "WEEKLY",
        },
        {
            "tradingsymbol": "NIFTY26SEP24200PE",
            "underlying": "NIFTY",
            "expiry": "2026-09-22",
            "strike": 24200,
            "option_type": "PE",
            "instrument_type": "OPTIDX",
            "expiry_class": "WEEKLY",
        },
    ]
    if lot_size is not None:
        for row in master:
            row["lot_size"] = lot_size
    return {
        "provider": "grow.stream.kite.market.v1",
        "adapter_version": "live_data.kite.market.v1",
        "sequence": sequence,
        "event_time": AS_OF.isoformat(),
        "received_time": AS_OF.isoformat(),
        "session_date": "2026-09-22",
        "source_timezone": "Asia/Kolkata",
        "instrument_type": "OPTIDX",
        "underlyings": ["NIFTY"],
        "spots": {"NIFTY": 24210.0},
        "contract_master": master,
        "option_quotes": quotes,
        "is_fixture": False,
    }


def _normalize(payload: dict):
    cfg = _live_config(provider="kite_market")
    return normalize_event(
        payload,
        now=AS_OF,
        max_staleness_seconds=cfg.live_data.max_staleness_seconds,
        calendar=SessionCalendar(cfg.market, clock=FrozenClock(AS_OF)),
    )


def _empty_package(snapshot) -> AggregateAnalysisPackage:
    return AggregateAnalysisPackage(
        cycle_id="cycle-mixed",
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
        paper_mode=True,
        live_trading=False,
    )


class ClassifyTests(unittest.TestCase):
    def test_fixture_flags_live_fixture_mixed(self) -> None:
        self.assertEqual(classify_fixture_flags([]), MarketDataSource.LIVE)
        self.assertEqual(classify_fixture_flags([False, False]), MarketDataSource.LIVE)
        self.assertEqual(classify_fixture_flags([True, True]), MarketDataSource.FIXTURE)
        self.assertEqual(classify_fixture_flags([True, False]), MarketDataSource.MIXED)

    def test_fixture_ce_plus_live_pe_is_never_live(self) -> None:
        snap = build_fixture_snapshot(underlying="NIFTY", as_of=AS_OF, spot=24210.0)
        mixed = replace(
            snap,
            option_contracts=(
                _option(option_type="CE", is_fixture=True),
                _option(option_type="PE", is_fixture=False),
            ),
            market_data_source=MarketDataSource.LIVE,
            is_fixture=False,
        )
        self.assertEqual(mixed.market_data_source, MarketDataSource.MIXED)
        self.assertFalse(mixed.is_fixture)
        self.assertEqual(classify_agent_snapshot(mixed), MarketDataSource.MIXED)
        self.assertEqual(reject_mixed_market_data(mixed), MIXED_MARKET_DATA_SOURCE)

    def test_all_live_options_classify_as_live(self) -> None:
        snap = build_fixture_snapshot(underlying="NIFTY", as_of=AS_OF, spot=24210.0)
        live = replace(
            snap,
            option_contracts=(
                _option(option_type="CE", is_fixture=False),
                _option(option_type="PE", is_fixture=False),
            ),
            is_fixture=False,
            market_data_source=MarketDataSource.LIVE,
            diagnostics={**dict(snap.diagnostics), "fixture": False, "market_data_source": "LIVE"},
            provider="grow.stream.kite.market.v1",
        )
        self.assertEqual(live.market_data_source, MarketDataSource.LIVE)
        self.assertFalse(live.is_fixture)
        self.assertIsNone(reject_mixed_market_data(live))

    def test_all_fixture_options_classify_as_fixture(self) -> None:
        snap = build_fixture_snapshot(
            underlying="NIFTY",
            as_of=AS_OF,
            spot=24210.0,
            option_contracts=(
                _option(option_type="CE", is_fixture=True),
                _option(option_type="PE", is_fixture=True),
            ),
        )
        self.assertEqual(snap.market_data_source, MarketDataSource.FIXTURE)
        self.assertTrue(snap.is_fixture)
        self.assertIsNone(reject_mixed_market_data(snap))


class NormalizeMixedTests(unittest.TestCase):
    def test_mixed_option_quotes_rejected_at_normalize(self) -> None:
        when = AS_OF.isoformat()
        payload = _kite_payload(
            quotes=[
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-09-22",
                    "strike": 24200,
                    "option_type": "CE",
                    "ts": when,
                    "ltp": 101.5,
                    "bid": 101.0,
                    "ask": 102.0,
                    "volume": 40,
                    "oi": 80,
                    "is_fixture": True,
                },
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-09-22",
                    "strike": 24200,
                    "option_type": "PE",
                    "ts": when,
                    "ltp": 88.0,
                    "bid": 87.5,
                    "ask": 88.5,
                    "volume": 12,
                    "oi": 30,
                    "is_fixture": False,
                },
            ]
        )
        with self.assertRaises(GrowConfigError) as ctx:
            _normalize(payload)
        self.assertEqual(str(ctx.exception), MIXED_MARKET_DATA_SOURCE)

    def test_all_fixture_quotes_on_live_provider_forbidden(self) -> None:
        when = AS_OF.isoformat()
        payload = _kite_payload(
            quotes=[
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-09-22",
                    "strike": 24200,
                    "option_type": "CE",
                    "ts": when,
                    "ltp": 101.5,
                    "bid": 101.0,
                    "ask": 102.0,
                    "is_fixture": True,
                },
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-09-22",
                    "strike": 24200,
                    "option_type": "PE",
                    "ts": when,
                    "ltp": 88.0,
                    "bid": 87.5,
                    "ask": 88.5,
                    "is_fixture": True,
                },
            ]
        )
        with self.assertRaises(GrowConfigError) as ctx:
            _normalize(payload)
        self.assertEqual(str(ctx.exception), "FIXTURE_FALLBACK_FORBIDDEN")

    def test_live_ce_pe_normalize_and_agent_snapshot_are_live(self) -> None:
        when = AS_OF.isoformat()
        payload = _kite_payload(
            quotes=[
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-09-22",
                    "strike": 24200,
                    "option_type": "CE",
                    "ts": when,
                    "ltp": 101.5,
                    "bid": 101.0,
                    "ask": 102.0,
                    "volume": 40,
                    "oi": 80,
                    "is_fixture": False,
                },
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-09-22",
                    "strike": 24200,
                    "option_type": "PE",
                    "ts": when,
                    "ltp": 88.0,
                    "bid": 87.5,
                    "ask": 88.5,
                    "volume": 12,
                    "oi": 30,
                    "is_fixture": False,
                },
            ]
        )
        live = _normalize(payload)
        agent = build_agent_snapshot(live, decision_timestamp=AS_OF, max_quote_age_seconds=30)
        self.assertEqual(agent.market_data_source, MarketDataSource.LIVE)
        self.assertFalse(agent.is_fixture)
        kinds = {row.option_type for row in agent.option_contracts}
        self.assertEqual(kinds, {"CE", "PE"})
        for row in agent.option_contracts:
            self.assertFalse(row.is_fixture)
            self.assertEqual(row.lot_size, 75)
            self.assertIsNotNone(row.bid)
            self.assertIsNotNone(row.ask)
            self.assertIsNotNone(row.ltp)
            self.assertEqual(getattr(row.quote_timestamp.tzinfo, "key", None), "Asia/Kolkata")


class PolicyAndPaperMixedTests(unittest.TestCase):
    def test_policy_blocks_mixed_snapshot(self) -> None:
        base = build_fixture_snapshot(underlying="NIFTY", as_of=AS_OF, spot=24210.0)
        mixed = replace(
            base,
            option_contracts=(
                _option(option_type="CE", is_fixture=True),
                _option(option_type="PE", is_fixture=False),
            ),
            data_quality=DataQualityStatus.OK,
            quality_notes=(),
            market_data_source=MarketDataSource.LIVE,
            is_fixture=False,
        )
        self.assertEqual(mixed.market_data_source, MarketDataSource.MIXED)
        result = evaluate_policy(mixed, _empty_package(mixed))
        self.assertEqual(result.terminal_status, "BLOCKED")
        self.assertIn(MIXED_MARKET_DATA_SOURCE, result.reason_codes)

    def test_paper_engine_rejects_mixed_before_fill(self) -> None:
        base = build_fixture_snapshot(underlying="NIFTY", as_of=AS_OF, spot=24210.0)
        mixed = replace(
            base,
            option_contracts=(
                _option(option_type="CE", is_fixture=True),
                _option(option_type="PE", is_fixture=False),
            ),
            data_quality=DataQualityStatus.OK,
            market_data_source=MarketDataSource.LIVE,
            is_fixture=False,
            provider="grow.stream.kite.market.v1",
        )
        decision = IntegratedDecision(
            decision_id="dec-mixed",
            analysis_cycle_id="cycle-mixed",
            snapshot_id=mixed.snapshot_id,
            snapshot_version=mixed.version,
            decision_timestamp=mixed.decision_timestamp,
            as_of=mixed.decision_timestamp,
            agent_output_refs=(),
            candidate_strategy="trend",
            candidate_instrument="NIFTY-CE-LIVE",
            direction="BULLISH",
            observations=(),
            calculated_evidence={
                "underlying": "NIFTY",
                "limit_price": 101.0,
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
        result = engine.execute(decision, mixed)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, MIXED_MARKET_DATA_SOURCE)


class QuoteValidationTests(unittest.TestCase):
    def test_missing_quote_keeps_contract_without_fabricating_prices(self) -> None:
        payload = _kite_payload(quotes=[])
        live = _normalize(payload)
        contracts = live.chains["NIFTY"].contracts
        self.assertEqual(len(contracts), 2)
        for row in contracts:
            self.assertIsNone(row.bid)
            self.assertIsNone(row.ask)
            self.assertIsNone(row.last_price)

    def test_invalid_quote_timestamp_rejected(self) -> None:
        payload = _kite_payload(
            quotes=[
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-09-22",
                    "strike": 24200,
                    "option_type": "CE",
                    "ts": "not-a-timestamp",
                    "ltp": 101.5,
                    "bid": 101.0,
                    "ask": 102.0,
                }
            ]
        )
        with self.assertRaises(GrowConfigError) as ctx:
            _normalize(payload)
        self.assertIn("INVALID_TIMESTAMP", str(ctx.exception))

    def test_naive_quote_timestamp_rejected(self) -> None:
        payload = _kite_payload(
            quotes=[
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-09-22",
                    "strike": 24200,
                    "option_type": "CE",
                    "ts": "2026-09-22T11:00:00",
                    "ltp": 101.5,
                    "bid": 101.0,
                    "ask": 102.0,
                }
            ]
        )
        with self.assertRaises(GrowConfigError) as ctx:
            _normalize(payload)
        self.assertIn("NAIVE_TIMESTAMP", str(ctx.exception))

    def test_missing_lot_size_preserves_none(self) -> None:
        when = AS_OF.isoformat()
        payload = _kite_payload(
            quotes=[
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-09-22",
                    "strike": 24200,
                    "option_type": "CE",
                    "ts": when,
                    "ltp": 101.5,
                    "bid": 101.0,
                    "ask": 102.0,
                }
            ],
            lot_size=None,
        )
        live = _normalize(payload)
        self.assertEqual(live.lot_sizes, {})
        agent = build_agent_snapshot(live, decision_timestamp=AS_OF)
        self.assertTrue(all(row.lot_size is None for row in agent.option_contracts))

    def test_invalid_lot_size_rejected(self) -> None:
        when = AS_OF.isoformat()
        payload = _kite_payload(
            quotes=[
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-09-22",
                    "strike": 24200,
                    "option_type": "CE",
                    "ts": when,
                    "ltp": 101.5,
                    "bid": 101.0,
                    "ask": 102.0,
                }
            ],
            lot_size=0,
        )
        with self.assertRaises(GrowConfigError) as ctx:
            _normalize(payload)
        self.assertEqual(str(ctx.exception), "INVALID_LOT_SIZE")


if __name__ == "__main__":
    unittest.main()
