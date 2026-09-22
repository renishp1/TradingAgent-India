"""Phase 7 — dedicated Signal Engine with counter-evidence schema."""

from __future__ import annotations

import unittest
from datetime import date, datetime

from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.contract import DecisionAction, IntegratedDecisionStatus
from grow.decision.integration.engine import DecisionEngine
from grow.decision.signal import SIGNAL_ENGINE_VERSION, SIGNAL_SCHEMA, CampaignSignal, SignalEngine
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.orchestration.models import AggregateAnalysisPackage

from tests.helpers import make_guard


AS_OF = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
EXPIRY = date(2026, 9, 24)


def _quote(**overrides):
    payload = dict(
        underlying="RELIANCE",
        expiry=EXPIRY,
        strike=2500.0,
        option_type="CE",
        ltp=100.0,
        bid=99.0,
        ask=101.0,
        open_interest=10,
        volume=10,
        quote_timestamp=AS_OF,
        quote_age_seconds=0.0,
        provider_contract_id="RELIANCE-2500-CE",
        quality=DataQualityStatus.OK,
        lot_size=1,
        expiry_class="WEEKLY",
    )
    payload.update(overrides)
    return OptionQuoteView(**payload)


def _snapshot(quotes=None):
    if quotes is None:
        quotes = (_quote(),)
    return build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=AS_OF,
        spot=2500.0,
        option_contracts=tuple(quotes),
    )


def _result(snapshot, *, instrument="RELIANCE-2500-CE", findings=("UNANIMOUS_OPEN",), **metric_overrides):
    metrics = {
        "strategy": "trend",
        "direction": "BULLISH",
        "underlying": "RELIANCE",
        "limit_price": 100.0,
        "stop_loss": 80.0,
        "lots": 1,
        "quantity": 1,
        "target": 140.0,
        "option_type": "CE",
        "strike": 2500.0,
        "expiry": EXPIRY.isoformat(),
        "lot_size": 1,
    }
    metrics.update(metric_overrides)
    return AgentResult(
        agent_name="strategy_research",
        agent_version="strategy_research.v2",
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        decision_timestamp=snapshot.decision_timestamp,
        status=AgentStatus.PASS,
        observations=("spot=2500",),
        calculated_metrics=metrics,
        interpretation=(),
        findings=findings,
        data_quality_concerns=(),
        assumptions=("buyer-only",),
        evidence=(f"snapshot_id={snapshot.snapshot_id}",),
        metrics_used=("limit_price", "stop_loss"),
        candidate_action=CandidateAction.PAPER_OPEN,
        candidate_instrument=instrument,
        entry_reason="test",
        invalidation_reason=None,
        risk_flags=(),
        missing_data=(),
        confidence=0.5,
        cycle_id="cycle-p7",
    )


def _package(snapshot, result, *, agreement=True, conflicts=(), dissenting=(), conflicting_evidence=()):
    return AggregateAnalysisPackage(
        cycle_id="cycle-p7",
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        as_of=snapshot.decision_timestamp,
        agent_outputs=(result,),
        rejected_outputs=(),
        dispatch_records=(),
        conflicts=tuple(conflicts),
        supporting_evidence=("pkg-support",),
        conflicting_evidence=tuple(conflicting_evidence),
        unavailable_agents=(),
        debate=DebateSummary(
            agreement=agreement,
            actions=("PAPER_OPEN",) if agreement else ("PAPER_OPEN", "HOLD"),
            agreeing_agents=(result.agent_name,) if agreement else (),
            dissenting_agents=tuple(dissenting),
            conflicts=tuple(conflicts),
            evidence=(),
            insufficient_agents=(),
            error_agents=(),
        ),
        cycle_summary="phase7",
        package_digest="pkg-phase7",
    )


def _engine() -> DecisionEngine:
    config = load_config()
    return DecisionEngine(config, risk_guard=make_guard(config, clock=FrozenClock(AS_OF)))


class SignalEngineUnitTests(unittest.TestCase):
    def test_buy_ce_with_supporting_evidence(self) -> None:
        snap = _snapshot()
        signal = SignalEngine().evaluate(snapshot=snap, package=_package(snap, _result(snap)))
        self.assertEqual(signal.action, DecisionAction.BUY_CE)
        self.assertEqual(signal.option_type, "CE")
        self.assertEqual(signal.schema_version, SIGNAL_SCHEMA)
        self.assertEqual(signal.engine_version, SIGNAL_ENGINE_VERSION)
        self.assertTrue(any("finding:" in item for item in signal.supporting_evidence))
        self.assertEqual(signal.counter_evidence, ())
        self.assertEqual(signal.reason_codes, ())
        payload = signal.to_dict()
        self.assertIn("counter_evidence", payload)
        self.assertTrue(payload["paper_mode"])
        self.assertFalse(payload["live_trading"])

    def test_buy_pe_for_bearish(self) -> None:
        snap = _snapshot(quotes=(_quote(option_type="PE", provider_contract_id="RELIANCE-2500-PE"),))
        result = _result(
            snap,
            instrument="RELIANCE-2500-PE",
            direction="BEARISH",
            option_type="PE",
            stop_loss=80.0,
            target=60.0,
        )
        signal = SignalEngine().evaluate(snapshot=snap, package=_package(snap, result))
        self.assertEqual(signal.action, DecisionAction.BUY_PE)
        self.assertEqual(signal.option_type, "PE")

    def test_debate_disagreement_is_no_trade_with_counter_evidence(self) -> None:
        snap = _snapshot()
        signal = SignalEngine().evaluate(
            snapshot=snap,
            package=_package(
                snap,
                _result(snap),
                agreement=False,
                conflicts=("ACTION_CONFLICT:PAPER_OPEN,HOLD",),
                dissenting=("other_agent",),
                conflicting_evidence=("agents disagree",),
            ),
        )
        self.assertEqual(signal.action, DecisionAction.NO_TRADE)
        codes = {item.code for item in signal.counter_evidence}
        self.assertIn("DEBATE_CONFLICT", codes)
        self.assertIn("DISSENTING_AGENT", codes)
        self.assertIn("CONFLICTING_EVIDENCE", codes)
        self.assertIn("DEBATE_DISAGREEMENT", signal.reason_codes)

    def test_chain_filter_rejection_records_counter_evidence(self) -> None:
        snap = _snapshot(quotes=(_quote(expiry_class=None),))
        signal = SignalEngine().evaluate(snapshot=snap, package=_package(snap, _result(snap)))
        self.assertEqual(signal.action, DecisionAction.NO_TRADE)
        self.assertTrue(any(item.code == "CHAIN_FILTER" for item in signal.counter_evidence))
        self.assertIn("UNKNOWN_EXPIRY_CLASS", signal.reason_codes)

    def test_no_actionable_agents_fail_closed(self) -> None:
        snap = _snapshot()
        result = _result(snap)
        # Force NONE action via reconstructing
        quiet = AgentResult(
            agent_name=result.agent_name,
            agent_version=result.agent_version,
            snapshot_id=result.snapshot_id,
            snapshot_version=result.snapshot_version,
            decision_timestamp=result.decision_timestamp,
            status=AgentStatus.PASS,
            observations=result.observations,
            calculated_metrics=result.calculated_metrics,
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
            confidence=0.1,
            cycle_id="cycle-p7",
        )
        signal = SignalEngine().evaluate(snapshot=snap, package=_package(snap, quiet, agreement=False, conflicts=()))
        self.assertEqual(signal.action, DecisionAction.NO_TRADE)
        self.assertIn("NO_ACTIONABLE_AGENTS", signal.reason_codes)


class DecisionEngineSignalAttachmentTests(unittest.TestCase):
    def test_decide_attaches_campaign_signal(self) -> None:
        snap = _snapshot()
        decision = _engine().decide(snapshot=snap, package=_package(snap, _result(snap)))
        self.assertEqual(decision.status, IntegratedDecisionStatus.CANDIDATE)
        self.assertEqual(decision.action, DecisionAction.BUY_CE)
        self.assertIsNotNone(decision.campaign_signal)
        self.assertEqual(decision.campaign_signal["action"], "BUY_CE")
        self.assertEqual(decision.campaign_signal["schema_version"], SIGNAL_SCHEMA)
        self.assertIn("campaign_signal", decision.to_dict())
        self.assertEqual(DecisionEngine.signal_of(decision)["engine_version"], SIGNAL_ENGINE_VERSION)

    def test_explain_matches_standalone_engine(self) -> None:
        snap = _snapshot()
        package = _package(snap, _result(snap))
        engine = _engine()
        explained = engine.explain(snapshot=snap, package=package)
        standalone = SignalEngine(load_config()).evaluate(snapshot=snap, package=package)
        self.assertEqual(explained.to_dict(), standalone.to_dict())
        self.assertIsInstance(explained, CampaignSignal)


if __name__ == "__main__":
    unittest.main()
