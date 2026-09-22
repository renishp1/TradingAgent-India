"""Orchestrator, risk hierarchy, and paper-only safety tests."""

from __future__ import annotations

import unittest
from datetime import datetime

from grow.agents.orchestrator import AgentCycleOrchestrator
from grow.agents.risk_guard import AgentRiskGuard
from grow.clock import IST
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.errors import GrowLiveTradingDisabled
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot


class _OpenAgent:
    agent_name = "force_open"
    agent_version = "force_open.v1"

    def analyze(self, snapshot):
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=("force open",),
            metrics_used=(),
            candidate_action=CandidateAction.PAPER_OPEN,
            candidate_instrument="NIFTY-25000-CE",
            entry_reason="test",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
        )


class _BoomAgent:
    agent_name = "boom"
    agent_version = "boom.v1"

    def analyze(self, snapshot):
        raise RuntimeError("boom")


def _snapshot(quality: DataQualityStatus = DataQualityStatus.OK):
    as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
    option = OptionQuoteView(
        underlying="NIFTY",
        expiry=as_of.date(),
        strike=25000.0,
        option_type="CE",
        ltp=120.0,
        bid=119.0,
        ask=121.0,
        open_interest=100,
        volume=10,
        quote_timestamp=as_of,
        quote_age_seconds=0.0,
        provider_contract_id="ce-1",
        quality=DataQualityStatus.OK,
    )
    return build_fixture_snapshot(
        underlying="NIFTY",
        as_of=as_of,
        spot=25000.0,
        option_contracts=(option,),
        quality=quality,
        notes=("stale",) if quality is DataQualityStatus.STALE else (),
    )


class AgentOrchestratorSafetyTests(unittest.TestCase):
    def test_default_cycle_is_no_trade_and_journaled(self) -> None:
        orch = AgentCycleOrchestrator(configured_strategies=("trend",))
        decision = orch.run(_snapshot())
        self.assertIn(decision.decision, {"NO_TRADE", "REJECTED_BY_RISK"})
        self.assertEqual(len(orch.journal.records), 1)
        self.assertEqual(decision.record.snapshot_id, decision.snapshot_id)
        self.assertFalse(decision.to_dict()["live_trading"])

    def test_quality_gate_short_circuits(self) -> None:
        orch = AgentCycleOrchestrator()
        decision = orch.run(_snapshot(DataQualityStatus.STALE))
        self.assertEqual(decision.decision, "NO_TRADE")
        self.assertTrue(decision.reason.startswith("DATA_QUALITY_GATE"))
        self.assertEqual(decision.agent_results, ())

    def test_risk_guard_rejects_despite_consensus(self) -> None:
        orch = AgentCycleOrchestrator(specialists=(_OpenAgent(), _OpenAgent()))
        decision = orch.run(_snapshot())
        self.assertEqual(decision.decision, "REJECTED_BY_RISK")
        self.assertFalse(decision.risk.approved)
        self.assertIn("CONSENSUS", decision.risk.reason)
        self.assertEqual(decision.paper_execution_result, "BLOCKED_BY_RISK")

    def test_specialist_failure_does_not_crash(self) -> None:
        orch = AgentCycleOrchestrator(specialists=(_BoomAgent(),))
        decision = orch.run(_snapshot())
        self.assertEqual(decision.agent_results[0].status, AgentStatus.ERROR)
        self.assertEqual(decision.decision, "NO_TRADE")
        self.assertIn("SPECIALIST_ERROR", decision.reason)

    def test_risk_guard_refuses_live_construction(self) -> None:
        with self.assertRaises(ValueError):
            AgentRiskGuard(live_trading=True)
        with self.assertRaises(GrowLiveTradingDisabled):
            AgentCycleOrchestrator(live_trading=True)

    def test_agents_cannot_enable_live_trading_constant(self) -> None:
        self.assertFalse(LIVE_TRADING_COMPILED)


if __name__ == "__main__":
    unittest.main()
