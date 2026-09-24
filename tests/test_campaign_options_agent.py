"""Campaign options agent + CEO gate + DTE scoring on campaign path."""

from __future__ import annotations

import unittest
from datetime import date, datetime

from grow.agents.campaign_options import CampaignOptionsAgent
from grow.clock import IST
from grow.decision.integration.campaign_score import rank_campaign_quotes, score_campaign_quote
from grow.decision.integration.ceo_gate import campaign_ceo_gate
from grow.decision.integration.contract import StrategyCandidate
from grow.decision.contracts.agent_result import CandidateAction
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.options.models import OptionType
from grow.config import load_config


AS_OF = datetime(2026, 9, 23, 11, 0, tzinfo=IST)
EXPIRY = date(2026, 9, 30)


def _quote(**overrides) -> OptionQuoteView:
    payload = dict(
        underlying="RELIANCE",
        expiry=EXPIRY,
        strike=2500.0,
        option_type="CE",
        ltp=100.0,
        bid=99.0,
        ask=101.0,
        open_interest=5_000,
        volume=1_000,
        quote_timestamp=AS_OF,
        quote_age_seconds=0.0,
        provider_contract_id="RELIANCE-2500-CE",
        quality=DataQualityStatus.OK,
        lot_size=1,
        expiry_class="WEEKLY",
        implied_volatility=0.18,
        delta=0.40,
        theta=-0.05,
    )
    payload.update(overrides)
    return OptionQuoteView(**payload)


class CampaignCeoGateTests(unittest.TestCase):
    def test_bullish_ce_approved(self) -> None:
        ok, reasons = campaign_ceo_gate(
            StrategyCandidate(
                strategy="campaign_options",
                instrument="RELIANCE-2500-CE",
                underlying="RELIANCE",
                direction="BULLISH",
                limit_price=101.0,
                stop_loss=80.0,
                quantity=1,
                confidence=0.5,
                option_type="CE",
                strike=2500.0,
                expiry=EXPIRY.isoformat(),
            )
        )
        self.assertTrue(ok)
        self.assertEqual(reasons, ())

    def test_bullish_pe_rejected(self) -> None:
        ok, reasons = campaign_ceo_gate(
            StrategyCandidate(
                strategy="campaign_options",
                instrument="RELIANCE-2500-PE",
                underlying="RELIANCE",
                direction="BULLISH",
                limit_price=101.0,
                stop_loss=80.0,
                quantity=1,
                confidence=0.5,
                option_type="PE",
                strike=2500.0,
                expiry=EXPIRY.isoformat(),
            )
        )
        self.assertFalse(ok)
        self.assertIn("BULLISH_NOT_CE", reasons)


class CampaignScoreTests(unittest.TestCase):
    def test_dte_score_prefers_near_week_over_same_dayish(self) -> None:
        cfg = load_config().options
        near = _quote(expiry=date(2026, 9, 30), provider_contract_id="NEAR-CE", open_interest=5_000)
        far = _quote(expiry=date(2026, 12, 30), provider_contract_id="FAR-CE", open_interest=5_000)
        near_score, near_diag = score_campaign_quote(
            near, spot=2500.0, atm=2500.0, as_of=AS_OF, option_type=OptionType.CE, config=cfg
        )
        far_score, far_diag = score_campaign_quote(
            far, spot=2500.0, atm=2500.0, as_of=AS_OF, option_type=OptionType.CE, config=cfg
        )
        self.assertGreater(near_diag["time_to_expiry"], far_diag["time_to_expiry"])
        self.assertFalse(near_diag["theta_in_score"])
        ranked = rank_campaign_quotes(
            (far, near),
            spot=2500.0,
            atm=2500.0,
            as_of=AS_OF,
            option_type=OptionType.CE,
            config=cfg,
        )
        assert ranked is not None
        self.assertEqual(ranked[0].provider_contract_id, "NEAR-CE")
        self.assertGreaterEqual(near_score, far_score)


class CampaignOptionsAgentTests(unittest.TestCase):
    def test_emits_paper_open_for_ce_only_chain(self) -> None:
        snap = build_fixture_snapshot(
            underlying="RELIANCE",
            as_of=AS_OF,
            spot=2500.0,
            option_contracts=(_quote(),),
        )
        result = CampaignOptionsAgent(load_config()).analyze(snap, cycle_id="c1")
        self.assertEqual(result.candidate_action, CandidateAction.PAPER_OPEN)
        self.assertEqual(result.calculated_metrics["direction"], "BULLISH")
        self.assertEqual(result.calculated_metrics["option_type"], "CE")
        self.assertEqual(result.calculated_metrics["lots"], 1)
        self.assertEqual(
            result.calculated_metrics["quantity"],
            result.calculated_metrics["lots"] * result.calculated_metrics["lot_size"],
        )
        self.assertIn("dte_days", result.calculated_metrics)
        self.assertFalse(result.calculated_metrics["theta_in_score"])

    def test_neutral_when_both_types_and_no_history(self) -> None:
        pe = _quote(
            option_type="PE",
            strike=2500.0,
            provider_contract_id="RELIANCE-2500-PE",
            delta=-0.40,
        )
        snap = build_fixture_snapshot(
            underlying="RELIANCE",
            as_of=AS_OF,
            spot=2500.0,
            option_contracts=(_quote(), pe),
        )
        result = CampaignOptionsAgent(load_config()).analyze(snap, cycle_id="c2")
        self.assertEqual(result.candidate_action, CandidateAction.NONE)
        self.assertIn("NO_DIRECTION", result.findings)


if __name__ == "__main__":
    unittest.main()
