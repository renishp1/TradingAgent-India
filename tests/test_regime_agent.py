"""RegimeAgent same-period direction — never Kite prior-day close vs today open."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from grow.agents.campaign_options import CampaignOptionsAgent
from grow.agents.regime import RegimeAgent
from grow.agents.technical import TechnicalAgent
from grow.clock import IST
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentStatus, CandidateAction
from grow.decision.integration.policy import _BEARISH, _BULLISH, _conflict_strings, evaluate_policy
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot, history_diagnostics_from_closes
from grow.orchestration.models import AggregateAnalysisPackage


AS_OF = datetime(2026, 9, 24, 11, 56, tzinfo=IST)


def _option(as_of: datetime, *, side: str, strike: float, ltp: float = 100.0) -> OptionQuoteView:
    return OptionQuoteView(
        underlying="NIFTY",
        expiry=as_of.date() + timedelta(days=12),
        strike=strike,
        option_type=side,
        ltp=ltp,
        bid=ltp - 1.0,
        ask=ltp + 1.0,
        open_interest=50,
        volume=20,
        quote_timestamp=as_of,
        quote_age_seconds=0.0,
        provider_contract_id=f"NIFTY-{strike:g}-{side}",
        quality=DataQualityStatus.OK,
        lot_size=65,
        is_fixture=False,
    )


def _gap_down_quote_snapshot(
    *,
    history: list[float] | None,
    ltp: float = 23201.05,
    today_open: float = 23221.8,
    prior_close: float = 23446.8,
    high: float = 23281.95,
    low: float = 23196.45,
):
    """Reproduce today's Kite OHLC semantics: close=prior day, open=today."""
    diag = {}
    if history is not None:
        diag = history_diagnostics_from_closes(
            history,
            interval="M15",
            earliest="2026-09-23T09:15:00+05:30",
            latest="2026-09-24T11:45:00+05:30",
            provenance="LIVE",
        )
    base = build_fixture_snapshot(
        underlying="NIFTY",
        as_of=AS_OF,
        spot=ltp,
        option_contracts=tuple(
            _option(AS_OF, side=side, strike=strike, ltp=150.0 if side == "PE" else 80.0)
            for strike in (ltp - 50, ltp, ltp + 50)
            for side in ("CE", "PE")
        ),
        diagnostics=diag,
    )
    quote = replace(
        base.underlyings["NIFTY"],
        ltp=ltp,
        spot=ltp,
        open=today_open,
        close=prior_close,
        high=high,
        low=low,
    )
    return replace(base, underlyings={"NIFTY": quote})


def _history_snap(closes: list[float], *, spot: float | None = None):
    px = float(closes[-1] if spot is None else spot)
    return build_fixture_snapshot(
        underlying="NIFTY",
        as_of=AS_OF,
        spot=px,
        option_contracts=(_option(AS_OF, side="CE", strike=px), _option(AS_OF, side="PE", strike=px)),
        diagnostics=history_diagnostics_from_closes(
            closes,
            interval="M15",
            earliest="2026-09-23T09:15:00+05:30",
            latest="2026-09-24T11:45:00+05:30",
            provenance="LIVE",
        ),
    )


def _package(snapshot, outputs):
    return AggregateAnalysisPackage(
        cycle_id="cycle-regime-fix",
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        as_of=snapshot.decision_timestamp,
        agent_outputs=tuple(outputs),
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
        cycle_summary="regime-fix",
        package_digest="digest-regime",
        paper_mode=True,
        live_trading=False,
    )


class RegimeAgentSamePeriodTests(unittest.TestCase):
    def test_gap_down_prior_close_does_not_force_trending_up(self) -> None:
        # Today's evidence: prior close > today open, but LTP below open + bearish M15.
        closes = [23300.0 - i * 4.0 for i in range(26)]  # declining M15 window
        closes[-1] = 23201.05
        snap = _gap_down_quote_snapshot(history=closes)
        result = RegimeAgent().analyze(snap, cycle_id="gap-down")
        self.assertNotIn("TRENDING_UP_CANDIDATE", result.findings)
        self.assertIn("TRENDING_DOWN_CANDIDATE", result.findings)
        self.assertEqual(result.calculated_metrics.get("regime_source"), "M15_HISTORY")
        # Prove prior-day close was present but must not drive the label.
        self.assertEqual(snap.underlyings["NIFTY"].close, 23446.8)
        self.assertEqual(snap.underlyings["NIFTY"].open, 23221.8)
        self.assertLess(snap.underlyings["NIFTY"].ltp, snap.underlyings["NIFTY"].open)
        self.assertGreater(snap.underlyings["NIFTY"].close, snap.underlyings["NIFTY"].open)

    def test_gap_down_without_history_uses_ltp_vs_today_open(self) -> None:
        snap = _gap_down_quote_snapshot(history=None)
        result = RegimeAgent().analyze(snap, cycle_id="gap-fallback")
        self.assertNotIn("TRENDING_UP_CANDIDATE", result.findings)
        self.assertIn("TRENDING_DOWN_CANDIDATE", result.findings)
        self.assertEqual(result.calculated_metrics.get("regime_source"), "LTP_VS_TODAY_OPEN")

    def test_same_period_bullish_m15(self) -> None:
        closes = [23000.0 + i * 5.0 for i in range(26)]
        result = RegimeAgent().analyze(_history_snap(closes), cycle_id="bull")
        self.assertEqual(result.status, AgentStatus.PASS)
        self.assertIn("TRENDING_UP_CANDIDATE", result.findings)
        self.assertEqual(result.calculated_metrics.get("regime_source"), "M15_HISTORY")

    def test_same_period_bearish_m15(self) -> None:
        closes = [23300.0 - i * 5.0 for i in range(26)]
        result = RegimeAgent().analyze(_history_snap(closes), cycle_id="bear")
        self.assertEqual(result.status, AgentStatus.PASS)
        self.assertIn("TRENDING_DOWN_CANDIDATE", result.findings)

    def test_insufficient_data_fail_closed(self) -> None:
        base = build_fixture_snapshot(
            underlying="NIFTY",
            as_of=AS_OF,
            spot=23200.0,
            option_contracts=(),
            diagnostics={},
        )
        quote = replace(base.underlyings["NIFTY"], open=None, close=None, high=None, low=None)
        snap = replace(base, underlyings={"NIFTY": quote})
        result = RegimeAgent().analyze(snap, cycle_id="insuff")
        self.assertEqual(result.status, AgentStatus.DEGRADED)
        self.assertIn("INSUFFICIENT_DATA", result.findings)
        self.assertIn("UNKNOWN", result.findings)
        self.assertEqual(result.candidate_action, CandidateAction.ABSTAIN)


class RegimeAgentUnchangedNeighborsTests(unittest.TestCase):
    def test_technical_unchanged_on_gap_down_history(self) -> None:
        closes = [23300.0 - i * 4.0 for i in range(30)]
        closes[-1] = 23201.05
        snap = _gap_down_quote_snapshot(history=closes)
        tech = TechnicalAgent().analyze(snap, cycle_id="tech")
        self.assertIn("SMA_FAST_BELOW_SLOW", tech.findings)
        self.assertNotIn("SMA_FAST_ABOVE_SLOW", tech.findings)

    def test_campaign_options_unchanged_bearish(self) -> None:
        closes = [23300.0 - i * 4.0 for i in range(30)]
        closes[-1] = 23201.05
        snap = _gap_down_quote_snapshot(history=closes)
        camp = CampaignOptionsAgent(load_config()).analyze(snap, cycle_id="camp")
        self.assertEqual((camp.calculated_metrics or {}).get("direction"), "BEARISH")

    def test_direction_conflict_token_tables_unchanged(self) -> None:
        self.assertEqual(_BULLISH, ("BULL", "TRENDING_UP", "SMA_FAST_ABOVE"))
        self.assertEqual(_BEARISH, ("BEAR", "TRENDING_DOWN", "SMA_FAST_BELOW"))


class GapDownIntegrationTests(unittest.TestCase):
    def test_today_scenario_no_false_regime_bullish_conflict(self) -> None:
        closes = [23300.0 - i * 4.0 for i in range(30)]
        closes[-1] = 23201.05
        snap = _gap_down_quote_snapshot(history=closes)
        regime = RegimeAgent().analyze(snap, cycle_id="c1")
        technical = TechnicalAgent().analyze(snap, cycle_id="c1")
        campaign = CampaignOptionsAgent(load_config()).analyze(snap, cycle_id="c1")

        self.assertNotIn("TRENDING_UP_CANDIDATE", regime.findings)
        self.assertIn("TRENDING_DOWN_CANDIDATE", regime.findings)
        self.assertIn("SMA_FAST_BELOW_SLOW", technical.findings)
        self.assertEqual((campaign.calculated_metrics or {}).get("direction"), "BEARISH")

        package = _package(snap, (regime, technical, campaign))
        conflicts = _conflict_strings(package, (regime, technical, campaign))
        self.assertFalse(
            any("DIRECTION_CONFLICT" in c and "bullish=regime" in c for c in conflicts),
            msg=f"unexpected false regime-bullish conflict: {conflicts}",
        )

        policy = evaluate_policy(snap, package, config=load_config())
        # May still NO_TRADE for other reasons, but not false bullish regime vs bearish tech.
        self.assertFalse(
            any("DIRECTION_CONFLICT:bullish=regime" in r for r in policy.reason_codes),
            msg=f"policy reasons: {policy.reason_codes}",
        )

    def test_live_trading_still_disabled(self) -> None:
        self.assertIs(LIVE_TRADING_COMPILED, False)


if __name__ == "__main__":
    unittest.main()
