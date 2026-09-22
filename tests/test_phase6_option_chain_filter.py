"""Phase 6 — campaign option-chain intelligence as sole candidate filter."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime

from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.chain_filter import (
    CHAIN_FILTER_REJECTED,
    CHAIN_FILTER_VERSION,
    CHAIN_UNIVERSE_EMPTY,
    MISSING_OPTION_CHAIN,
    UNKNOWN_EXPIRY_CLASS,
    _expiry_class,
    allow_campaign_candidate,
    filter_campaign_chain,
)
from grow.decision.integration.contract import IntegratedDecisionStatus, StrategyCandidate
from grow.decision.integration.engine import DecisionEngine
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.options.models import ExpiryClass
from grow.orchestration.models import AggregateAnalysisPackage

from tests.helpers import make_guard


AS_OF = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
EXPIRY = date(2026, 9, 24)


def _quote(*, strike: float = 2500.0, option_type: str = "CE", bid: float = 99.0, ask: float = 101.0, **overrides):
    payload = dict(
        underlying="RELIANCE",
        expiry=EXPIRY,
        strike=strike,
        option_type=option_type,
        ltp=100.0,
        bid=bid,
        ask=ask,
        open_interest=10,
        volume=10,
        quote_timestamp=AS_OF,
        quote_age_seconds=0.0,
        provider_contract_id=f"RELIANCE-{int(strike)}-{option_type}",
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


def _candidate(**overrides):
    payload = dict(
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
        lot_size=1,
    )
    payload.update(overrides)
    return StrategyCandidate(**payload)


def _package(snapshot, result):
    return AggregateAnalysisPackage(
        cycle_id="cycle-p6",
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        as_of=snapshot.decision_timestamp,
        agent_outputs=(result,),
        rejected_outputs=(),
        dispatch_records=(),
        conflicts=(),
        supporting_evidence=(),
        conflicting_evidence=(),
        unavailable_agents=(),
        debate=DebateSummary(
            agreement=True,
            actions=("PAPER_OPEN",),
            agreeing_agents=(result.agent_name,),
            dissenting_agents=(),
            conflicts=(),
            evidence=(),
            insufficient_agents=(),
            error_agents=(),
        ),
        cycle_summary="phase6",
        package_digest="pkg-phase6",
    )


def _result(snapshot, *, instrument="RELIANCE-2500-CE", **metric_overrides):
    metrics = {
        "strategy": "trend",
        "direction": "BULLISH",
        "underlying": "RELIANCE",
        "limit_price": 100.0,
        "stop_loss": 80.0,
        "lots": 1,
        "quantity": 1,
        "target": 140.0,
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
        findings=("UNANIMOUS_OPEN",),
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
        cycle_id="cycle-p6",
    )


def _engine() -> DecisionEngine:
    config = load_config()
    return DecisionEngine(config, risk_guard=make_guard(config, clock=FrozenClock(AS_OF)))


class ChainFilterUnitTests(unittest.TestCase):
    def test_atm_ce_is_eligible(self) -> None:
        snap = _snapshot()
        result = filter_campaign_chain(snap, underlying="RELIANCE", direction="BULLISH")
        self.assertTrue(result.ok)
        self.assertEqual(result.diagnostics["filter_version"], CHAIN_FILTER_VERSION)
        self.assertIn("RELIANCE-2500-CE", result.eligible_instruments)
        code, _ = allow_campaign_candidate(snap, _candidate())
        self.assertIsNone(code)

    def test_missing_chain_fails_closed(self) -> None:
        snap = _snapshot(quotes=())
        result = filter_campaign_chain(snap, underlying="RELIANCE", direction="BULLISH")
        self.assertEqual(result.reason_codes, (MISSING_OPTION_CHAIN,))

    def test_outside_atm_window_rejected(self) -> None:
        # Spot 2500; far strike 4000 with a grid so window excludes it.
        quotes = (
            _quote(strike=2500.0),
            _quote(strike=2550.0),
            _quote(strike=2600.0),
            _quote(strike=4000.0),
        )
        snap = _snapshot(quotes=quotes)
        code, filtered = allow_campaign_candidate(
            snap,
            _candidate(instrument="RELIANCE-4000-CE", strike=4000.0),
        )
        self.assertEqual(code, CHAIN_FILTER_REJECTED)
        self.assertNotIn("RELIANCE-4000-CE", filtered.eligible_instruments)

    def test_wide_spread_empties_universe(self) -> None:
        snap = _snapshot(quotes=(_quote(bid=10.0, ask=50.0, ltp=30.0),))
        result = filter_campaign_chain(snap, underlying="RELIANCE", direction="BULLISH")
        self.assertEqual(result.reason_codes, (CHAIN_UNIVERSE_EMPTY,))
        self.assertTrue(any("WIDE_SPREAD" in item for item in result.diagnostics["rejected"]))

    def test_bearish_requires_pe(self) -> None:
        snap = _snapshot(quotes=(_quote(option_type="PE", strike=2500.0),))
        result = filter_campaign_chain(snap, underlying="RELIANCE", direction="BEARISH")
        self.assertTrue(result.ok)
        self.assertTrue(all(row.option_type == "PE" for row in result.eligible_quotes))

    def test_expiry_class_none_rejected(self) -> None:
        snap = _snapshot(quotes=(_quote(expiry_class=None),))
        result = filter_campaign_chain(snap, underlying="RELIANCE", direction="BULLISH")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_codes, (UNKNOWN_EXPIRY_CLASS,))
        self.assertEqual(UNKNOWN_EXPIRY_CLASS, "UNKNOWN_EXPIRY_CLASS")
        self.assertIsNone(result.selected_expiry)
        self.assertIsNone(_expiry_class(None))

    def test_expiry_class_unknown_rejected(self) -> None:
        snap = _snapshot(quotes=(_quote(expiry_class="UNKNOWN"),))
        result = filter_campaign_chain(snap, underlying="RELIANCE", direction="BULLISH")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_codes, (UNKNOWN_EXPIRY_CLASS,))
        self.assertIsNone(result.selected_expiry)
        self.assertIsNone(_expiry_class("UNKNOWN"))

    def test_expiry_class_invalid_rejected(self) -> None:
        snap = _snapshot(quotes=(_quote(expiry_class="INVALID"),))
        result = filter_campaign_chain(snap, underlying="RELIANCE", direction="BULLISH")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_codes, (UNKNOWN_EXPIRY_CLASS,))
        self.assertIsNone(result.selected_expiry)
        self.assertIsNone(_expiry_class("INVALID"))

    def test_expiry_class_weekly_accepted(self) -> None:
        snap = _snapshot(quotes=(_quote(expiry_class="WEEKLY"),))
        result = filter_campaign_chain(snap, underlying="RELIANCE", direction="BULLISH")
        self.assertTrue(result.ok)
        self.assertEqual(result.selected_expiry, EXPIRY.isoformat())
        self.assertIs(ExpiryClass.WEEKLY, _expiry_class("WEEKLY"))

    def test_expiry_class_monthly_accepted(self) -> None:
        # Classification accepts MONTHLY; use monthly-preferred config so choose_expiry can select it.
        opts = replace(load_config().options, preferred_expiry_class="monthly")
        snap = _snapshot(quotes=(_quote(expiry_class="MONTHLY"),))
        result = filter_campaign_chain(snap, underlying="RELIANCE", direction="BULLISH", config=opts)
        self.assertTrue(result.ok)
        self.assertEqual(result.selected_expiry, EXPIRY.isoformat())
        self.assertNotIn(UNKNOWN_EXPIRY_CLASS, result.reason_codes)
        self.assertIs(ExpiryClass.MONTHLY, _expiry_class("MONTHLY"))

    def test_expiry_class_lowercase_weekly_accepted(self) -> None:
        snap = _snapshot(quotes=(_quote(expiry_class="weekly"),))
        result = filter_campaign_chain(snap, underlying="RELIANCE", direction="BULLISH")
        self.assertTrue(result.ok)
        self.assertEqual(result.selected_expiry, EXPIRY.isoformat())
        self.assertIs(_expiry_class("weekly"), ExpiryClass.WEEKLY)

    def test_expiry_class_lowercase_monthly_accepted(self) -> None:
        opts = replace(load_config().options, preferred_expiry_class="monthly")
        snap = _snapshot(quotes=(_quote(expiry_class="monthly"),))
        result = filter_campaign_chain(snap, underlying="RELIANCE", direction="BULLISH", config=opts)
        self.assertTrue(result.ok)
        self.assertEqual(result.selected_expiry, EXPIRY.isoformat())
        self.assertIs(_expiry_class("monthly"), ExpiryClass.MONTHLY)

    def test_expiry_class_helper_never_fabricates_weekly(self) -> None:
        """Regression: None/UNKNOWN/invalid must not become ExpiryClass.WEEKLY."""
        for raw in (None, "UNKNOWN", "UNKNOWN_EXPIRY_CLASS", "INVALID", "", "  ", "QUARTERLY"):
            klass = _expiry_class(raw)
            self.assertIsNone(klass, msg=repr(raw))
            self.assertIsNot(klass, ExpiryClass.WEEKLY)
        self.assertIs(_expiry_class("WEEKLY"), ExpiryClass.WEEKLY)
        self.assertIs(_expiry_class("MONTHLY"), ExpiryClass.MONTHLY)
        self.assertIs(_expiry_class("weekly"), ExpiryClass.WEEKLY)
        self.assertIs(_expiry_class("monthly"), ExpiryClass.MONTHLY)
        source = open("grow/decision/integration/chain_filter.py", encoding="utf-8").read()
        self.assertNotIn("if raw is None:\n        return ExpiryClass.WEEKLY", source)
        self.assertNotIn("except ValueError:\n        return ExpiryClass.WEEKLY", source)
        self.assertIn('UNKNOWN_EXPIRY_CLASS = "UNKNOWN_EXPIRY_CLASS"', source)
        # Allowlist lookup only — no direct fabricate return of WEEKLY for bad inputs.
        self.assertIn("_ALLOWED_EXPIRY_CLASS", source)

    def test_unclassified_expiry_cannot_produce_eligible_candidate(self) -> None:
        """Option chain with None/invalid expiry_class must not yield an eligible instrument."""
        for bad in (None, "UNKNOWN", "INVALID"):
            with self.subTest(expiry_class=bad):
                snap = _snapshot(quotes=(_quote(expiry_class=bad),))
                filtered = filter_campaign_chain(snap, underlying="RELIANCE", direction="BULLISH")
                self.assertEqual(filtered.reason_codes, (UNKNOWN_EXPIRY_CLASS,))
                self.assertEqual(filtered.eligible_instruments, ())
                self.assertEqual(filtered.eligible_quotes, ())
                self.assertIsNone(filtered.selected_expiry)
                code, again = allow_campaign_candidate(snap, _candidate())
                self.assertEqual(code, UNKNOWN_EXPIRY_CLASS)
                self.assertFalse(again.ok)
                decision = _engine().decide(
                    snapshot=snap,
                    package=_package(snap, _result(snap)),
                )
                self.assertEqual(decision.status, IntegratedDecisionStatus.NO_TRADE)
                self.assertIn(UNKNOWN_EXPIRY_CLASS, decision.reason_codes)


class DecisionEngineChainFilterTests(unittest.TestCase):
    def test_approved_candidate_records_chain_filter_gate(self) -> None:
        snap = _snapshot()
        decision = _engine().decide(snapshot=snap, package=_package(snap, _result(snap)))
        self.assertEqual(decision.status, IntegratedDecisionStatus.CANDIDATE)
        gates = {gate: (passed, detail) for gate, passed, detail in decision.gate_results}
        self.assertTrue(gates["option_chain_filter"][0])
        self.assertIn("eligible=", gates["option_chain_filter"][1])

    def test_far_strike_is_no_trade(self) -> None:
        quotes = (
            _quote(strike=2500.0),
            _quote(strike=2550.0),
            _quote(strike=2600.0),
            _quote(strike=4000.0),
        )
        snap = _snapshot(quotes=quotes)
        decision = _engine().decide(
            snapshot=snap,
            package=_package(snap, _result(snap, instrument="RELIANCE-4000-CE")),
        )
        self.assertEqual(decision.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertEqual(decision.action.value, "NO_TRADE")
        self.assertIn(CHAIN_FILTER_REJECTED, decision.reason_codes)

    def test_agents_cannot_bypass_chain_with_arbitrary_instrument(self) -> None:
        snap = _snapshot()
        decision = _engine().decide(
            snapshot=snap,
            package=_package(snap, _result(snap, instrument="RELIANCE-FAKE-CE")),
        )
        self.assertEqual(decision.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertTrue(
            any(code in decision.reason_codes for code in {CHAIN_FILTER_REJECTED, "CRITICAL_DATA_MISSING", "INCOMPLETE_CANDIDATE"})
        )


if __name__ == "__main__":
    unittest.main()
