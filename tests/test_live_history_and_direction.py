"""Regression: LIVE M15 history_closes + structured direction extraction.

Does not place broker orders. Does not change scoring / CEO / RiskGuard.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from grow.agents.campaign_options import CampaignOptionsAgent
from grow.agents.technical import TechnicalAgent, _history_closes
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.live_data.kite_market import (
    HISTORY_KITE_INTERVAL,
    HISTORY_MIN_CLOSES,
    HISTORY_TIMEFRAME,
    candles_to_m15_spot_bars,
    synthetic_m15_candles,
)
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.provenance import MarketDataSource
from grow.market_data.snapshots.builder import (
    build_agent_snapshot,
    build_fixture_snapshot,
    history_diagnostics_from_closes,
)
from grow.orchestration.aggregator import aggregate_outputs, direction_vote
from tests.test_live_data import AS_OF
from tests.test_zerodha_market import _frame, _full_packet, _index_packet, _normalize, _provider, CE_TOKEN, PE_TOKEN


def _option(as_of: datetime, *, side: str = "CE", strike: float = 23200.0) -> OptionQuoteView:
    return OptionQuoteView(
        underlying="NIFTY",
        expiry=as_of.date() + timedelta(days=5),
        strike=strike,
        option_type=side,
        ltp=100.0,
        bid=99.0,
        ask=101.0,
        open_interest=10,
        volume=5,
        quote_timestamp=as_of,
        quote_age_seconds=0.0,
        provider_contract_id=f"NIFTY-{strike:g}-{side}",
        quality=DataQualityStatus.OK,
        lot_size=65,
        is_fixture=False,
    )


def _agent(
    name: str,
    *,
    findings: tuple[str, ...] = (),
    interpretation: tuple[str, ...] = (),
    observations: tuple[str, ...] = (),
    metrics: dict | None = None,
    status: AgentStatus = AgentStatus.PASS,
) -> AgentResult:
    return AgentResult(
        agent_name=name,
        agent_version=f"{name}.v1",
        snapshot_id="snap-1",
        snapshot_version="v1",
        decision_timestamp=AS_OF,
        status=status,
        observations=observations,
        calculated_metrics=metrics or {},
        interpretation=interpretation,
        findings=findings,
        data_quality_concerns=(),
        assumptions=(),
        evidence=("e",),
        metrics_used=(),
        candidate_action=CandidateAction.NONE,
        candidate_instrument=None,
        entry_reason=None,
        invalidation_reason=None,
        risk_flags=(),
        missing_data=(),
        cycle_id="c1",
    )


class LiveHistoryIntegrationTests(unittest.TestCase):
    def test_history_interval_is_explicit_m15(self) -> None:
        self.assertEqual(HISTORY_TIMEFRAME, "M15")
        self.assertEqual(HISTORY_KITE_INTERVAL, "15minute")
        self.assertGreaterEqual(HISTORY_MIN_CLOSES, 15)

    def test_live_snapshot_carries_sufficient_m15_history(self) -> None:
        when = AS_OF - timedelta(seconds=2)
        provider = _provider(
            [
                _frame(_index_packet(24210.0, AS_OF)),
                _frame(_full_packet(CE_TOKEN, ltp=101.5, bid=101.0, ask=102.0, volume=40, oi=80, when=when)),
                _frame(_full_packet(PE_TOKEN, ltp=99.5, bid=99.0, ask=100.0, volume=30, oi=70, when=when)),
            ]
        )
        provider.poll()  # index
        provider.poll()  # CE
        payload = provider.poll()  # PE (+ assembled)
        self.assertTrue(payload.get("spot_bars"), msg="expected M15 spot_bars from historical fetch")
        self.assertTrue(all(row["timeframe"] == "M15" for row in payload["spot_bars"]))
        self.assertGreaterEqual(len(payload["spot_bars"]), HISTORY_MIN_CLOSES)

        live = _normalize(payload)
        agent = build_agent_snapshot(live, decision_timestamp=AS_OF, max_quote_age_seconds=30)
        diag = dict(agent.diagnostics)
        self.assertEqual(agent.market_data_source, MarketDataSource.LIVE)
        self.assertEqual(diag.get("market_data_source"), "LIVE")
        self.assertEqual(diag.get("history_provenance"), "LIVE")
        self.assertEqual(diag.get("history_interval"), "M15")
        self.assertGreaterEqual(int(diag.get("history_bar_count") or 0), 15)
        self.assertGreaterEqual(len(diag.get("history_closes") or ()), 15)
        self.assertTrue(diag.get("history_earliest"))
        self.assertTrue(diag.get("history_latest"))
        self.assertFalse(agent.is_fixture)

    def test_technical_uses_history_closes_not_ohlc_fallback(self) -> None:
        closes = [100.0 + i for i in range(20)]
        snap = build_fixture_snapshot(
            underlying="NIFTY",
            as_of=AS_OF,
            spot=999.0,
            option_contracts=(_option(AS_OF),),
            diagnostics=history_diagnostics_from_closes(
                closes,
                interval="M15",
                earliest="2026-09-01T09:15:00+05:30",
                latest="2026-09-22T15:15:00+05:30",
                provenance="LIVE",
            ),
        )
        used = _history_closes(snap, snap.underlyings["NIFTY"])
        self.assertEqual(list(used), closes)
        # Fixture OHLC are all equal to spot=999; history must win over that 5-field fallback.
        fallback = [
            float(v)
            for v in (
                snap.underlyings["NIFTY"].open,
                snap.underlyings["NIFTY"].high,
                snap.underlyings["NIFTY"].low,
                snap.underlyings["NIFTY"].close,
                snap.underlyings["NIFTY"].ltp,
            )
            if v is not None
        ]
        self.assertNotEqual(list(used), fallback)
        result = TechnicalAgent().analyze(snap, cycle_id="hist-tech")
        self.assertNotIn("INSUFFICIENT_HISTORY", result.findings)
        self.assertGreaterEqual(int(result.calculated_metrics.get("history_bars") or 0), 15)

    def test_campaign_options_can_compute_sma_direction(self) -> None:
        closes = [100.0 + i * 0.5 for i in range(20)]
        atm = closes[-1]
        snap = build_fixture_snapshot(
            underlying="NIFTY",
            as_of=AS_OF,
            spot=atm,
            option_contracts=tuple(
                _option(AS_OF, side=side, strike=strike)
                for strike in (atm - 50, atm, atm + 50)
                for side in ("CE", "PE")
            ),
            diagnostics=history_diagnostics_from_closes(
                closes,
                interval="M15",
                earliest="2026-09-01T09:15:00+05:30",
                latest="2026-09-22T15:15:00+05:30",
                provenance="LIVE",
            ),
        )
        result = CampaignOptionsAgent().analyze(snap, cycle_id="hist-camp")
        self.assertNotIn("NO_DIRECTION", result.findings)
        self.assertTrue(any(o.startswith("direction=") and "NEUTRAL" not in o for o in result.observations))
        direction = (result.calculated_metrics or {}).get("direction")
        self.assertIn(direction, {"BULLISH", "BEARISH"})

    def test_candles_helper_preserves_m15(self) -> None:
        candles = synthetic_m15_candles(as_of=AS_OF, start_px=24000.0, count=80)
        self.assertGreaterEqual(len(candles), 15)
        bars = candles_to_m15_spot_bars(candles, underlying="NIFTY")
        self.assertEqual(bars[0]["timeframe"], "M15")
        self.assertEqual(len(bars), len(candles))


class DirectionExtractionTests(unittest.TestCase):
    def test_neutral_campaign_options_does_not_vote_bullish_or_bearish(self) -> None:
        row = _agent(
            "campaign_options",
            findings=("NO_DIRECTION",),
            interpretation=("No bullish/bearish structure; no campaign PAPER_OPEN.",),
            observations=("underlying=NIFTY", "direction=NEUTRAL"),
        )
        self.assertIsNone(direction_vote(row))

    def test_prose_bull_bear_words_do_not_create_conflict(self) -> None:
        campaign = _agent(
            "campaign_options",
            findings=("NO_DIRECTION",),
            interpretation=("No bullish/bearish structure; no campaign PAPER_OPEN.",),
            observations=("direction=NEUTRAL",),
        )
        regime = _agent(
            "regime",
            findings=("TRENDING_UP_CANDIDATE",),
            interpretation=("Close above open with modest range — provisional uptrend candidate.",),
        )
        package = aggregate_outputs(
            cycle_id="c",
            snapshot_id="s",
            snapshot_version="v",
            as_of=AS_OF,
            outputs=(regime, campaign),
            rejected_outputs=(),
            dispatch_records=(),
        )
        self.assertFalse(any(c.startswith("DIRECTION_CONFLICT") for c in package.conflicts))

    def test_structured_bullish_vs_bearish_still_conflicts(self) -> None:
        bull = _agent("bull", findings=("TRENDING_UP_CANDIDATE",), interpretation=("bullish structure",))
        bear = _agent("bear", findings=("TRENDING_DOWN_CANDIDATE",), interpretation=("bearish structure",))
        package = aggregate_outputs(
            cycle_id="c",
            snapshot_id="s",
            snapshot_version="v",
            as_of=AS_OF,
            outputs=(bull, bear),
            rejected_outputs=(),
            dispatch_records=(),
        )
        conflicts = [c for c in package.conflicts if c.startswith("DIRECTION_CONFLICT")]
        self.assertEqual(len(conflicts), 1)
        self.assertIn("bullish=bull", conflicts[0])
        self.assertIn("bearish=bear", conflicts[0])

    def test_metrics_direction_bullish_and_bearish(self) -> None:
        self.assertEqual(
            direction_vote(_agent("c", findings=("CAMPAIGN_OPTION_CANDIDATE",), metrics={"direction": "BULLISH"})),
            "BULLISH",
        )
        self.assertEqual(
            direction_vote(_agent("c", findings=("CAMPAIGN_OPTION_CANDIDATE",), metrics={"direction": "BEARISH"})),
            "BEARISH",
        )

    def test_fail_closed_conflict_policy_unchanged(self) -> None:
        package = aggregate_outputs(
            cycle_id="c",
            snapshot_id="s",
            snapshot_version="v",
            as_of=AS_OF,
            outputs=(_agent("a", findings=("BULLISH",)), _agent("b", findings=("BEARISH",))),
            rejected_outputs=(),
            dispatch_records=(),
        )
        self.assertTrue(any(c.startswith("DIRECTION_CONFLICT") for c in package.conflicts))


class SafetyInvariantTests(unittest.TestCase):
    def test_live_trading_compiled_false(self) -> None:
        self.assertFalse(LIVE_TRADING_COMPILED)

    def test_kite_market_has_no_order_route(self) -> None:
        from pathlib import Path

        text = Path("grow/live_data/kite_market.py").read_text(encoding="utf-8")
        self.assertNotIn("place_order", text)
        self.assertNotIn("/orders", text)
        self.assertIn("fetch_historical_candles", text)
        self.assertNotIn("broker_order_calls = 1", text)


if __name__ == "__main__":
    unittest.main()
