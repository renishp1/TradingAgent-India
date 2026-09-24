"""CampaignOptions early cash/risk feasibility filter (same formulas as RiskGuard)."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from uuid import uuid4

from grow.agents.campaign_options import (
    NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE,
    CampaignOptionsAgent,
    evaluate_campaign_feasibility,
    premium_stop_loss,
)
from grow.campaign import CampaignRunner, campaign_paper_config
from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.decision.contracts.agent_result import CandidateAction
from grow.decision.integration.contract import IntegratedDecisionStatus
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.types import Intent, MarketBrief, Regime, SessionState, Side, Symbol, TradeProposal, Venue

from tests.helpers import TEST_RISK_SECRET, make_guard


AS_OF = datetime(2026, 9, 24, 11, 0, tzinfo=IST)
EXPIRY = date(2026, 10, 6)


def _cfg():
    return campaign_paper_config(load_config(environ={"GROW_EXECUTION_MODE": "paper"}))


def _quote(**overrides) -> OptionQuoteView:
    """Tight bid/ask so chain-filter liquidity gates pass; feasibility is what's under test."""
    ask = float(overrides.get("ask", 100.0) or 100.0)
    payload = dict(
        underlying="NIFTY",
        expiry=EXPIRY,
        strike=23100.0,
        option_type="PE",
        ltp=ask,
        bid=round(ask - 0.5, 2),
        ask=ask,
        open_interest=1_000,
        volume=200,
        quote_timestamp=AS_OF,
        quote_age_seconds=0.0,
        provider_contract_id="NIFTY-23100-PE",
        quality=DataQualityStatus.OK,
        lot_size=65,
        expiry_class="WEEKLY",
        is_fixture=False,
    )
    payload.update(overrides)
    if "ask" in overrides and overrides["ask"] is not None and "bid" not in overrides:
        a = float(overrides["ask"])
        payload["ask"] = a
        payload["bid"] = round(a - 0.5, 2)
        payload["ltp"] = a if overrides.get("ltp") is None else overrides.get("ltp", a)
    return OptionQuoteView(**payload)


def _pe_only_snap(*quotes: OptionQuoteView, spot: float = 23100.0):
    # PE-only chain → CampaignOptions sparse-fallback BEARISH.
    return build_fixture_snapshot(
        underlying="NIFTY",
        as_of=AS_OF,
        spot=spot,
        option_contracts=quotes,
    )


class FeasibilityUnitTests(unittest.TestCase):
    def test_1_atm_exceeds_cash_rejected(self) -> None:
        # 182 * 65 = 11830 > 10000
        row = evaluate_campaign_feasibility(
            _quote(ask=182.0, lot_size=65),
            lots=1,
            available_cash=10_000.0,
            max_per_trade_risk=1_000.0,
        )
        self.assertFalse(row.ok)
        self.assertEqual(row.reject_reason, "CASH")
        self.assertGreater(row.required_cash, 10_000.0)

    def test_2_cash_ok_risk_exceeded_rejected(self) -> None:
        # 142.6 * 65 = 9269 <= 10000; planned 0.2*142.6*65 ≈ 1854 > 1000
        row = evaluate_campaign_feasibility(
            _quote(ask=142.6, lot_size=65, provider_contract_id="NIFTY-23000-PE", strike=23000.0),
            lots=1,
            available_cash=10_000.0,
            max_per_trade_risk=1_000.0,
        )
        self.assertFalse(row.ok)
        self.assertEqual(row.reject_reason, "RISK")
        self.assertLessEqual(row.required_cash, 10_000.0)
        self.assertGreater(row.planned_risk, 1_000.0)

    def test_3_both_constraints_accepted(self) -> None:
        # ask 10 * 65 = 650 cash; planned 0.2*10*65 = 130 risk
        row = evaluate_campaign_feasibility(
            _quote(ask=10.0, ltp=10.0, lot_size=65, provider_contract_id="NIFTY-CHEAP-PE"),
            lots=1,
            available_cash=10_000.0,
            max_per_trade_risk=1_000.0,
        )
        self.assertTrue(row.ok)
        self.assertIsNone(row.reject_reason)
        self.assertEqual(row.stop_loss, premium_stop_loss(10.0))

    def test_5_missing_lot_size_rejected(self) -> None:
        row = evaluate_campaign_feasibility(
            _quote(ask=10.0, lot_size=None),
            lots=1,
            available_cash=10_000.0,
            max_per_trade_risk=1_000.0,
        )
        self.assertFalse(row.ok)
        self.assertEqual(row.reject_reason, "MISSING_LOT_SIZE")

    def test_6_missing_ask_rejected(self) -> None:
        row = evaluate_campaign_feasibility(
            _quote(ask=None, ltp=None),
            lots=1,
            available_cash=10_000.0,
            max_per_trade_risk=1_000.0,
        )
        self.assertFalse(row.ok)
        self.assertEqual(row.reject_reason, "MISSING_ASK")


class FeasibilityAgentTests(unittest.TestCase):
    def test_4_no_qualifying_candidate_structured_no_trade(self) -> None:
        # All expensive / high-risk vs ₹10k / ₹1k.
        snap = _pe_only_snap(
            _quote(ask=182.0, strike=23100.0, provider_contract_id="ATM-PE"),
            _quote(ask=142.6, strike=23000.0, provider_contract_id="OTM-PE"),
        )
        result = CampaignOptionsAgent(_cfg()).analyze(snap, cycle_id="none")
        self.assertEqual(result.candidate_action, CandidateAction.NONE)
        self.assertIn(NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE, result.findings)
        self.assertEqual(result.invalidation_reason, NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE)
        metrics = result.calculated_metrics
        for key in (
            "candidates_checked",
            "candidates_rejected_cash",
            "candidates_rejected_risk",
            "minimum_cash_required",
            "minimum_planned_risk",
            "available_cash",
            "max_per_trade_risk",
        ):
            self.assertIn(key, metrics)
        self.assertEqual(metrics["available_cash"], 10_000.0)
        self.assertEqual(metrics["max_per_trade_risk"], 1_000.0)
        self.assertGreaterEqual(metrics["candidates_checked"], 1)

    def test_3_agent_emits_when_affordable(self) -> None:
        snap = _pe_only_snap(
            _quote(ask=10.0, ltp=10.0, bid=9.5, strike=23100.0, provider_contract_id="CHEAP-PE", lot_size=65),
        )
        result = CampaignOptionsAgent(_cfg()).analyze(snap, cycle_id="ok")
        self.assertEqual(result.candidate_action, CandidateAction.PAPER_OPEN)
        self.assertEqual(result.candidate_instrument, "CHEAP-PE")
        self.assertLessEqual(result.calculated_metrics["required_cash"], 10_000.0)
        self.assertLessEqual(result.calculated_metrics["planned_risk"], 1_000.0)

    def test_1_agent_rejects_cash_exceeding_atm(self) -> None:
        snap = _pe_only_snap(_quote(ask=182.0, strike=23100.0, provider_contract_id="ATM-PE"))
        result = CampaignOptionsAgent(_cfg()).analyze(snap, cycle_id="cash")
        self.assertEqual(result.candidate_action, CandidateAction.NONE)
        self.assertIn(NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE, result.findings)
        self.assertGreaterEqual(result.calculated_metrics["candidates_rejected_cash"], 1)

    def test_10_11_locks(self) -> None:
        self.assertIs(LIVE_TRADING_COMPILED, False)


class FeasibilityRiskGuardIntegrationTests(unittest.TestCase):
    def test_7_risk_guard_still_validates_final_candidate(self) -> None:
        cfg = _cfg()
        snap = _pe_only_snap(
            _quote(ask=10.0, ltp=10.0, bid=9.5, strike=23100.0, provider_contract_id="CHEAP-PE", lot_size=65),
        )
        agent = CampaignOptionsAgent(cfg).analyze(snap, cycle_id="rg")
        self.assertEqual(agent.candidate_action, CandidateAction.PAPER_OPEN)
        m = agent.calculated_metrics
        clock = FrozenClock(AS_OF)
        guard = make_guard(cfg, clock=clock)
        proposal = TradeProposal(
            proposal_id=f"t-{uuid4().hex[:8]}",
            symbol=Symbol("NIFTY"),
            side=Side.BUY,
            intent=Intent.OPEN,
            quantity=int(m["quantity"]),
            limit_price=float(m["limit_price"]),
            stop_loss=float(m["stop_loss"]),
            take_profit=float(m["target"]),
            thesis="feasibility-integration",
            confidence=0.5,
            venue=Venue.PAPER,
            created_at=AS_OF,
            notional=round(float(m["limit_price"]) * int(m["quantity"]), 2),
        )
        brief = MarketBrief(
            symbol=Symbol("NIFTY"),
            as_of=AS_OF,
            session=SessionState.OPEN,
            last_price=float(m["limit_price"]),
            currency="INR",
            regime=Regime.UNKNOWN,
            notes=(),
            source="test",
        )
        verdict = guard.evaluate(
            proposal,
            brief,
            cash=float(cfg.paper.starting_cash),
            gross_notional=0.0,
            daily_pnl=0.0,
            symbol_notional=0.0,
            open_positions=0,
        )
        self.assertTrue(verdict.approved, verdict.reason)
        universe = [r for r in verdict.rule_results if r[0] == "symbol.universe"][0]
        cash = [r for r in verdict.rule_results if r[0] == "cash.available"][0]
        risk = [r for r in verdict.rule_results if r[0] == "risk.per_trade"][0]
        self.assertTrue(universe[1])
        self.assertTrue(cash[1])
        self.assertTrue(risk[1])

    def test_campaign_runner_no_broker_on_unaffordable(self) -> None:
        cfg = _cfg()
        clock = FrozenClock(AS_OF)
        runner = CampaignRunner(
            cfg,
            clock=clock,
            risk_secret=TEST_RISK_SECRET,
            specialists=(CampaignOptionsAgent(cfg),),
            apply_campaign_defaults=False,
        )
        snap = _pe_only_snap(_quote(ask=182.0, provider_contract_id="ATM-PE"))
        result = runner.run_cycle(snap, cycle_id="no-fill")
        self.assertNotEqual(result.decision.status, IntegratedDecisionStatus.CANDIDATE)
        self.assertFalse(result.execution.accepted)
        self.assertEqual(result.execution.broker_order_calls, 0)
        self.assertIs(LIVE_TRADING_COMPILED, False)


if __name__ == "__main__":
    unittest.main()
