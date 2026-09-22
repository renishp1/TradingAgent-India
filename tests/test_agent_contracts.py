"""Tests for structured agent contracts and debate aggregation (4B)."""

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


def _snap(*, with_options: bool = True, quality: DataQualityStatus = DataQualityStatus.OK, history=None):
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
    diagnostics = {}
    if history is not None:
        diagnostics["history_closes"] = list(history)
    return build_fixture_snapshot(
        underlying="NIFTY",
        as_of=as_of,
        spot=25000.0,
        option_contracts=options,
        quality=quality,
        notes=("unit",) if quality is not DataQualityStatus.OK else (),
        diagnostics=diagnostics,
    )


class AgentContractTests(unittest.TestCase):
    def test_specialists_return_structured_results(self) -> None:
        snap = _snap(history=[100 + i for i in range(20)])
        agents = (
            MarketDataAgent(),
            TechnicalAgent(),
            OptionsChainAgent(),
            RegimeAgent(),
            StrategyResearchAgent(("trend",)),
        )
        for agent in agents:
            result = agent.analyze(snap, cycle_id="cycle-test")
            self.assertEqual(result.snapshot_id, snap.snapshot_id)
            self.assertEqual(result.snapshot_version, snap.version)
            self.assertEqual(result.cycle_id, "cycle-test")
            self.assertEqual(result.decision_timestamp, snap.decision_timestamp)
            self.assertIn(result.status, set(AgentStatus))
            self.assertIsInstance(result.observations, tuple)
            self.assertIsInstance(dict(result.calculated_metrics), dict)
            self.assertIsInstance(result.interpretation, tuple)
            self.assertIsInstance(result.findings, tuple)
            self.assertIsInstance(result.assumptions, tuple)
            self.assertIsInstance(result.evidence, tuple)
            payload = result.to_dict()
            self.assertFalse(payload["live_trading"])
            self.assertFalse(payload["executed"])
            self.assertEqual(payload["schema_version"], "grow.agent.result.v2")

    def test_missing_options_is_no_data(self) -> None:
        snap = _snap(with_options=False)
        result = OptionsChainAgent().analyze(snap)
        self.assertEqual(result.status, AgentStatus.NO_DATA)
        self.assertIn("option_contracts", result.missing_data)

    def test_stale_snapshot_blocks_agents(self) -> None:
        snap = _snap(quality=DataQualityStatus.STALE)
        result = MarketDataAgent().analyze(snap)
        self.assertEqual(result.status, AgentStatus.NO_DATA)

    def test_insufficient_history_is_explicit(self) -> None:
        snap = _snap(history=[1.0, 2.0, 3.0])
        result = TechnicalAgent().analyze(snap)
        self.assertEqual(result.status, AgentStatus.DEGRADED)
        self.assertIn("INSUFFICIENT_HISTORY", result.findings)

    def test_conflicting_recommendations_preserved(self) -> None:
        as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
        left = AgentResult(
            agent_name="a",
            agent_version="v1",
            snapshot_id="s1",
            snapshot_version="v",
            decision_timestamp=as_of,
            status=AgentStatus.PASS,
            observations=("buy",),
            calculated_metrics={},
            interpretation=("bullish",),
            findings=("BULLISH",),
            data_quality_concerns=(),
            assumptions=(),
            evidence=("e1",),
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
            snapshot_version="v",
            decision_timestamp=as_of,
            status=AgentStatus.PASS,
            observations=("sell",),
            calculated_metrics={},
            interpretation=("bearish",),
            findings=("BEARISH",),
            data_quality_concerns=(),
            assumptions=(),
            evidence=("e2",),
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

    def test_no_data_requires_missing_data(self) -> None:
        as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
        with self.assertRaises(ValueError):
            AgentResult(
                agent_name="x",
                agent_version="v1",
                snapshot_id="s1",
                snapshot_version="v",
                decision_timestamp=as_of,
                status=AgentStatus.NO_DATA,
                observations=(),
                calculated_metrics={},
                interpretation=(),
                findings=(),
                data_quality_concerns=(),
                assumptions=(),
                evidence=(),
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
