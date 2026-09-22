"""Tests for structured agent contracts and debate aggregation."""

from __future__ import annotations

import unittest
from datetime import datetime

from grow.agents.market_data import MarketDataAgent
from grow.agents.options import OptionsChainAgent
from grow.agents.regime import RegimeAgent
from grow.agents.strategy_research import StrategyResearchAgent
from grow.agents.technical import TechnicalAgent
from grow.clock import IST
from grow.decision.aggregation.debate import summarize_debate
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot


def _snap(*, with_options: bool = True, quality: DataQualityStatus = DataQualityStatus.OK):
    as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
    options = ()
    if with_options:
        options = (
            OptionQuoteView(
                underlying="NIFTY",
                expiry=as_of.date(),
                strike=25000.0,
                option_type="CE",
                ltp=120.0,
                bid=119.0,
                ask=121.0,
                open_interest=100,
                volume=20,
                quote_timestamp=as_of,
                quote_age_seconds=0.0,
                provider_contract_id="ce-1",
                quality=DataQualityStatus.OK,
            ),
        )
    return build_fixture_snapshot(
        underlying="NIFTY",
        as_of=as_of,
        spot=25000.0,
        option_contracts=options,
        quality=quality,
        notes=("unit",) if quality is not DataQualityStatus.OK else (),
    )


class AgentContractTests(unittest.TestCase):
    def test_specialists_return_structured_results(self) -> None:
        snap = _snap()
        agents = (
            MarketDataAgent(),
            TechnicalAgent(),
            OptionsChainAgent(),
            RegimeAgent(),
            StrategyResearchAgent(("trend",)),
        )
        for agent in agents:
            result = agent.analyze(snap)
            self.assertEqual(result.snapshot_id, snap.snapshot_id)
            self.assertEqual(result.decision_timestamp, snap.decision_timestamp)
            self.assertIn(result.status, set(AgentStatus))
            payload = result.to_dict()
            self.assertFalse(payload["live_trading"])
            self.assertFalse(payload["executed"])

    def test_missing_options_is_data_insufficient(self) -> None:
        snap = _snap(with_options=False)
        result = OptionsChainAgent().analyze(snap)
        self.assertEqual(result.status, AgentStatus.DATA_INSUFFICIENT)
        self.assertIn("option_contracts", result.missing_data)

    def test_stale_snapshot_blocks_agents(self) -> None:
        snap = _snap(quality=DataQualityStatus.STALE)
        result = MarketDataAgent().analyze(snap)
        self.assertEqual(result.status, AgentStatus.DATA_INSUFFICIENT)

    def test_conflicting_recommendations_preserved(self) -> None:
        as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
        left = AgentResult(
            agent_name="a",
            agent_version="v1",
            snapshot_id="s1",
            decision_timestamp=as_of,
            status=AgentStatus.PASS,
            observations=("buy",),
            metrics_used=(),
            candidate_action=CandidateAction.PAPER_OPEN,
            candidate_instrument="NIFTY-CE",
            entry_reason="momentum",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
        )
        right = AgentResult(
            agent_name="b",
            agent_version="v1",
            snapshot_id="s1",
            decision_timestamp=as_of,
            status=AgentStatus.PASS,
            observations=("sell",),
            metrics_used=(),
            candidate_action=CandidateAction.PAPER_CLOSE,
            candidate_instrument="NIFTY-PE",
            entry_reason="mean-reversion",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
        )
        debate = summarize_debate((left, right))
        self.assertFalse(debate.agreement)
        self.assertTrue(debate.conflicts)
        self.assertIn("a:PAPER_OPEN:momentum", debate.evidence)
        self.assertIn("b:PAPER_CLOSE:mean-reversion", debate.evidence)

    def test_data_insufficient_requires_missing_data(self) -> None:
        as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
        with self.assertRaises(ValueError):
            AgentResult(
                agent_name="x",
                agent_version="v1",
                snapshot_id="s1",
                decision_timestamp=as_of,
                status=AgentStatus.DATA_INSUFFICIENT,
                observations=(),
                metrics_used=(),
                candidate_action=CandidateAction.NONE,
                candidate_instrument=None,
                entry_reason=None,
                invalidation_reason=None,
                risk_flags=(),
                missing_data=(),
            )


if __name__ == "__main__":
    unittest.main()
