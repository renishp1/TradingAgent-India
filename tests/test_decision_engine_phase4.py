"""Phase 4 — Decision Engine BUY_CE / BUY_PE / NO_TRADE + TradeCandidate provenance."""

from __future__ import annotations

import unittest
from datetime import date, datetime

from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.contract import (
    DECISION_SCHEMA,
    DecisionAction,
    DecisionBookState,
    IntegratedDecisionStatus,
    StrategyCandidate,
    TradeCandidate,
    build_trade_candidate,
    resolve_decision_action,
)
from grow.decision.integration.engine import DecisionEngine
from grow.decision.integration.integrator import DecisionIntegrator
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.orchestration.models import AggregateAnalysisPackage

from tests.helpers import make_guard


AS_OF = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
EXPIRY = date(2026, 9, 24)


def _quote(*, option_type: str = "CE", strike: float = 2500.0, underlying: str = "RELIANCE"):
    return OptionQuoteView(
        underlying=underlying,
        expiry=EXPIRY,
        strike=strike,
        option_type=option_type,
        ltp=100.0,
        bid=99.0,
        ask=101.0,
        open_interest=10,
        volume=10,
        quote_timestamp=AS_OF,
        quote_age_seconds=0.0,
        provider_contract_id=f"{underlying}-{int(strike)}-{option_type}",
        quality=DataQualityStatus.OK,
        lot_size=75,
    )


def _snapshot(*, quotes=None, quality: DataQualityStatus = DataQualityStatus.OK):
    if quotes is None:
        quotes = (_quote(),)
    return build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=AS_OF,
        spot=2500.0,
        option_contracts=tuple(quotes),
        quality=quality,
    )


def _metrics(**overrides):
    payload = {
        "strategy": "trend",
        "direction": "BULLISH",
        "underlying": "RELIANCE",
        "limit_price": 100.0,
        "stop_loss": 80.0,
        "target": 140.0,
        "quantity": 1,
    }
    payload.update(overrides)
    return payload


def _result(snapshot, *, instrument="RELIANCE-2500-CE", name="strategy_research", **kwargs):
    metrics = kwargs.pop("metrics", _metrics())
    return AgentResult(
        agent_name=name,
        agent_version=kwargs.pop("version", f"{name}.v2"),
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        decision_timestamp=snapshot.decision_timestamp,
        status=AgentStatus.PASS,
        observations=("spot=2500",),
        calculated_metrics=metrics,
        interpretation=(),
        findings=("UNANIMOUS_OPEN",),
        data_quality_concerns=(),
        assumptions=("buyer-only",),
        evidence=(f"snapshot_id={snapshot.snapshot_id}",),
        metrics_used=("limit_price", "stop_loss", "target"),
        candidate_action=CandidateAction.PAPER_OPEN,
        candidate_instrument=instrument,
        entry_reason="test",
        invalidation_reason=None,
        risk_flags=(),
        missing_data=(),
        confidence=0.5,
        cycle_id=kwargs.pop("cycle_id", "cycle-p4"),
    )


def _package(snapshot, outputs):
    if isinstance(outputs, AgentResult):
        outputs = (outputs,)
    return AggregateAnalysisPackage(
        cycle_id="cycle-p4",
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
            agreement=True,
            actions=("PAPER_OPEN",),
            agreeing_agents=tuple(row.agent_name for row in outputs),
            dissenting_agents=(),
            conflicts=(),
            evidence=(),
            insufficient_agents=(),
            error_agents=(),
        ),
        cycle_summary="phase4",
        package_digest="pkg-phase4",
    )


def _engine(**risk_overrides) -> DecisionEngine:
    config = load_config()
    if risk_overrides:
        from dataclasses import replace

        config = replace(config, risk=replace(config.risk, **risk_overrides))
    guard = make_guard(config, clock=FrozenClock(AS_OF))
    return DecisionEngine(config, risk_guard=guard)


class DecisionActionHelpersTests(unittest.TestCase):
    def test_resolve_maps_candidate_ce_pe_and_closed(self) -> None:
        self.assertEqual(
            resolve_decision_action(IntegratedDecisionStatus.CANDIDATE, option_type="CE"),
            DecisionAction.BUY_CE,
        )
        self.assertEqual(
            resolve_decision_action(IntegratedDecisionStatus.CANDIDATE, option_type="PE"),
            DecisionAction.BUY_PE,
        )
        self.assertEqual(
            resolve_decision_action(IntegratedDecisionStatus.NO_TRADE, option_type="CE"),
            DecisionAction.NO_TRADE,
        )
        self.assertEqual(
            resolve_decision_action(IntegratedDecisionStatus.BLOCKED, option_type="PE"),
            DecisionAction.NO_TRADE,
        )
        self.assertEqual(
            resolve_decision_action(IntegratedDecisionStatus.CANDIDATE),
            DecisionAction.NO_TRADE,
        )

    def test_trade_candidate_requires_option_provenance(self) -> None:
        base = StrategyCandidate(
            strategy="trend",
            instrument="RELIANCE-2500-CE",
            underlying="RELIANCE",
            direction="BULLISH",
            limit_price=100.0,
            stop_loss=80.0,
            quantity=1,
            confidence=0.5,
            option_type="CE",
            strike=2500.0,
            expiry=EXPIRY.isoformat(),
            target=140.0,
            lot_size=75,
        )
        tc = build_trade_candidate(
            base,
            package_digest="pkg",
            agent_versions=(("strategy_research", "strategy_research.v2"),),
            snapshot_id="snap",
            snapshot_version="v1",
            analysis_cycle_id="cycle-p4",
        )
        assert tc is not None
        self.assertEqual(tc.action, DecisionAction.BUY_CE)
        self.assertEqual(tc.strike, 2500.0)
        self.assertEqual(tc.expiry, EXPIRY.isoformat())
        self.assertEqual(tc.target, 140.0)
        self.assertEqual(tc.package_digest, "pkg")
        self.assertEqual(tc.agent_versions[0], ("strategy_research", "strategy_research.v2"))

        incomplete = StrategyCandidate(
            strategy="trend",
            instrument="RELIANCE-2500-CE",
            underlying="RELIANCE",
            direction="BULLISH",
            limit_price=100.0,
            stop_loss=80.0,
            quantity=1,
            confidence=0.5,
            option_type="CE",
        )
        self.assertIsNone(
            build_trade_candidate(
                incomplete,
                package_digest="pkg",
                agent_versions=(),
                snapshot_id="snap",
                snapshot_version="v1",
                analysis_cycle_id="cycle-p4",
            )
        )


class DecisionEnginePhase4Tests(unittest.TestCase):
    def test_schema_is_v2(self) -> None:
        self.assertEqual(DECISION_SCHEMA, "grow.decision.integration.v2")

    def test_buy_ce_with_full_trade_candidate_provenance(self) -> None:
        snap = _snapshot()
        decision = _engine().decide(snapshot=snap, package=_package(snap, (_result(snap),)))
        self.assertEqual(decision.status, IntegratedDecisionStatus.CANDIDATE)
        self.assertEqual(decision.action, DecisionAction.BUY_CE)
        self.assertIsInstance(decision.trade_candidate, TradeCandidate)
        tc = decision.trade_candidate
        assert tc is not None
        self.assertEqual(tc.option_type, "CE")
        self.assertEqual(tc.strike, 2500.0)
        self.assertEqual(tc.expiry, EXPIRY.isoformat())
        self.assertEqual(tc.stop_loss, 80.0)
        self.assertEqual(tc.target, 140.0)
        self.assertEqual(tc.lot_size, 75)
        self.assertTrue(tc.package_digest)
        self.assertTrue(tc.agent_versions)
        self.assertEqual(tc.snapshot_id, snap.snapshot_id)
        self.assertEqual(tc.analysis_cycle_id, "cycle-p4")
        self.assertEqual(decision.schema_version, DECISION_SCHEMA)
        self.assertEqual(decision.to_dict()["action"], "BUY_CE")
        self.assertEqual(decision.to_dict()["trade_candidate"]["strike"], 2500.0)

    def test_buy_pe_for_bearish_put(self) -> None:
        quotes = (_quote(option_type="PE", strike=2400.0),)
        snap = _snapshot(quotes=quotes)
        result = _result(
            snap,
            instrument="RELIANCE-2400-PE",
            metrics=_metrics(direction="BEARISH", stop_loss=80.0, target=60.0),
        )
        decision = _engine().decide(snapshot=snap, package=_package(snap, (result,)))
        self.assertEqual(decision.status, IntegratedDecisionStatus.CANDIDATE)
        self.assertEqual(decision.action, DecisionAction.BUY_PE)
        assert decision.trade_candidate is not None
        self.assertEqual(decision.trade_candidate.option_type, "PE")
        self.assertEqual(decision.trade_candidate.strike, 2400.0)
        self.assertEqual(decision.trade_candidate.target, 60.0)

    def test_no_trade_when_data_stale(self) -> None:
        snap = _snapshot(quality=DataQualityStatus.STALE)
        decision = _engine().decide(snapshot=snap, package=_package(snap, (_result(snap),)))
        self.assertEqual(decision.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertEqual(decision.action, DecisionAction.NO_TRADE)
        self.assertIsNone(decision.trade_candidate)

    def test_blocked_risk_maps_action_to_no_trade(self) -> None:
        snap = _snapshot()
        decision = _engine().decide(
            snapshot=snap,
            package=_package(snap, (_result(snap),)),
            book=DecisionBookState(
                cash=10_000.0,
                gross_notional=0.0,
                daily_pnl=-20_000.0,
                symbol_notional=0.0,
                open_positions=0,
            ),
        )
        self.assertEqual(decision.status, IntegratedDecisionStatus.BLOCKED)
        self.assertEqual(decision.action, DecisionAction.NO_TRADE)
        self.assertIsNone(decision.trade_candidate)

    def test_option_instrument_without_resolvable_quote_is_incomplete(self) -> None:
        snap = _snapshot(quotes=())
        decision = _engine().decide(
            snapshot=snap,
            package=_package(snap, (_result(snap, instrument="RELIANCE-2500-CE"),)),
        )
        self.assertEqual(decision.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertEqual(decision.action, DecisionAction.NO_TRADE)
        self.assertIn("INCOMPLETE_CANDIDATE", decision.reason_codes)

    def test_engine_replay_stable(self) -> None:
        snap = _snapshot()
        engine = _engine()
        first = engine.decide(snapshot=snap, package=_package(snap, (_result(snap),)))
        replayed = engine.replay(first.decision_id)
        self.assertEqual(replayed.to_dict(), first.to_dict())
        self.assertEqual(DecisionEngine.action_of(replayed), DecisionAction.BUY_CE)

    def test_integrator_still_emits_action_fields(self) -> None:
        snap = _snapshot()
        config = load_config()
        guard = make_guard(config, clock=FrozenClock(AS_OF))
        decision = DecisionIntegrator(config, risk_guard=guard).integrate(
            snapshot=snap,
            package=_package(snap, (_result(snap),)),
        )
        self.assertEqual(decision.action, DecisionAction.BUY_CE)
        self.assertIsNotNone(decision.trade_candidate)


if __name__ == "__main__":
    unittest.main()
