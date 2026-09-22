"""Phase 7 — dedicated Signal Engine with counter-evidence schema."""

from __future__ import annotations

import ast
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.contract import DecisionAction, DecisionBookState, IntegratedDecisionStatus
from grow.decision.integration.engine import DecisionEngine
from grow.decision.signal import SIGNAL_ENGINE_VERSION, SIGNAL_SCHEMA, CampaignSignal, SignalEngine
from grow.decision.signal.engine import _candidate_from_agent
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.orchestration.models import AggregateAnalysisPackage

from tests.helpers import make_guard


AS_OF = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
EXPIRY = date(2026, 9, 24)
ROOT = Path(__file__).resolve().parents[1]


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


class CandidateFromAgentFailClosedTests(unittest.TestCase):
    """Malformed agent metrics must fail closed — never raise into decide()."""

    def _assert_incomplete_no_trade(self, **metric_overrides) -> CampaignSignal:
        snap = _snapshot()
        signal = SignalEngine().evaluate(
            snapshot=snap,
            package=_package(snap, _result(snap, **metric_overrides)),
        )
        self.assertEqual(signal.action, DecisionAction.NO_TRADE)
        self.assertIn("INCOMPLETE_SIGNAL_CANDIDATE", signal.reason_codes)
        self.assertTrue(
            any(item.code == "INCOMPLETE_SIGNAL_CANDIDATE" for item in signal.counter_evidence)
        )
        return signal

    def test_invalid_quantity_string_is_no_trade(self) -> None:
        self._assert_incomplete_no_trade(quantity="abc")

    def test_invalid_quantity_none_is_no_trade(self) -> None:
        self._assert_incomplete_no_trade(quantity=None)

    def test_invalid_strike_string_is_no_trade(self) -> None:
        self._assert_incomplete_no_trade(strike="abc")

    def test_invalid_target_string_is_no_trade(self) -> None:
        self._assert_incomplete_no_trade(target="abc")

    def test_invalid_lot_size_string_is_no_trade(self) -> None:
        self._assert_incomplete_no_trade(lot_size="abc")

    def test_invalid_limit_price_string_is_no_trade(self) -> None:
        self._assert_incomplete_no_trade(limit_price="abc")

    def test_invalid_stop_loss_string_is_no_trade(self) -> None:
        self._assert_incomplete_no_trade(stop_loss="abc")

    def test_malformed_numeric_combinations_is_no_trade(self) -> None:
        self._assert_incomplete_no_trade(
            quantity="abc",
            strike="abc",
            target="abc",
            lot_size="abc",
            limit_price="abc",
            stop_loss="abc",
        )

    def test_invalid_lots_when_quantity_absent_is_no_trade(self) -> None:
        snap = _snapshot()
        base = _result(snap)
        metrics = {key: value for key, value in base.calculated_metrics.items() if key != "quantity"}
        metrics["lots"] = "abc"
        result = replace(base, calculated_metrics=metrics)
        signal = SignalEngine().evaluate(snapshot=snap, package=_package(snap, result))
        self.assertEqual(signal.action, DecisionAction.NO_TRADE)
        self.assertIn("INCOMPLETE_SIGNAL_CANDIDATE", signal.reason_codes)

    def test_decide_malformed_metrics_returns_no_trade_not_raise(self) -> None:
        """DecisionEngine.decide() stays deterministic NO_TRADE; never raises."""
        snap = _snapshot()
        # Required / quantity-path malformations: integrator also fails closed → NO_TRADE.
        required_cases = (
            {"quantity": "abc"},
            {"quantity": None},
            {"lot_size": "abc"},
            {"limit_price": "abc"},
            {"stop_loss": "abc"},
            {
                "quantity": "abc",
                "strike": "abc",
                "target": "abc",
                "lot_size": "abc",
                "limit_price": "abc",
                "stop_loss": "abc",
            },
        )
        for overrides in required_cases:
            with self.subTest(overrides=overrides):
                engine = _engine()
                decision = engine.decide(
                    snapshot=snap,
                    package=_package(snap, _result(snap, **overrides)),
                )
                self.assertEqual(decision.action, DecisionAction.NO_TRADE)
                self.assertEqual(decision.campaign_signal["action"], "NO_TRADE")
                self.assertIn(
                    "INCOMPLETE_SIGNAL_CANDIDATE",
                    decision.campaign_signal["reason_codes"],
                )
                again = _engine().decide(
                    snapshot=snap,
                    package=_package(snap, _result(snap, **overrides)),
                )
                self.assertEqual(decision.action, again.action)
                self.assertEqual(
                    decision.campaign_signal["reason_codes"],
                    again.campaign_signal["reason_codes"],
                )
                self.assertEqual(
                    decision.campaign_signal["signal_id"],
                    again.campaign_signal["signal_id"],
                )

        # Optional numeric malformations: SignalEngine fails closed; decide must not raise.
        for overrides in ({"strike": "abc"}, {"target": "abc"}):
            with self.subTest(optional=overrides):
                engine = _engine()
                decision = engine.decide(
                    snapshot=snap,
                    package=_package(snap, _result(snap, **overrides)),
                )
                self.assertEqual(decision.campaign_signal["action"], "NO_TRADE")
                self.assertIn(
                    "INCOMPLETE_SIGNAL_CANDIDATE",
                    decision.campaign_signal["reason_codes"],
                )


class CandidateFromAgentNormalizationTests(unittest.TestCase):
    """String metrics must normalize to typed numeric StrategyCandidate fields."""

    def _candidate(self, *, confidence=0.5, drop_quantity: bool = False, **metric_overrides):
        snap = _snapshot()
        base = _result(snap, **metric_overrides)
        metrics = dict(base.calculated_metrics)
        if drop_quantity:
            metrics.pop("quantity", None)
        result = replace(base, calculated_metrics=metrics, confidence=confidence)
        return _candidate_from_agent(result)

    def test_quantity_string_normalizes_to_int(self) -> None:
        candidate = self._candidate(quantity="10")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.quantity, 10)
        self.assertIsInstance(candidate.quantity, int)

    def test_lots_string_normalizes_to_quantity_int(self) -> None:
        candidate = self._candidate(lots="2", drop_quantity=True)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.quantity, 2)
        self.assertIsInstance(candidate.quantity, int)

    def test_strike_string_normalizes_to_float(self) -> None:
        candidate = self._candidate(strike="25000")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.strike, 25000.0)
        self.assertIsInstance(candidate.strike, float)

    def test_target_string_normalizes_to_float(self) -> None:
        candidate = self._candidate(target="180")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.target, 180.0)
        self.assertIsInstance(candidate.target, float)

    def test_lot_size_string_normalizes_to_int(self) -> None:
        candidate = self._candidate(lot_size="75")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.lot_size, 75)
        self.assertIsInstance(candidate.lot_size, int)

    def test_limit_price_string_normalizes_to_float(self) -> None:
        candidate = self._candidate(limit_price="100.5")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.limit_price, 100.5)
        self.assertIsInstance(candidate.limit_price, float)

    def test_stop_loss_string_normalizes_to_float(self) -> None:
        candidate = self._candidate(stop_loss="90.5")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.stop_loss, 90.5)
        self.assertIsInstance(candidate.stop_loss, float)

    def test_confidence_string_normalizes_to_float(self) -> None:
        # AgentResult rejects non-float confidence; exercise the parser path directly.
        snap = _snapshot()
        base = _result(snap)
        row = replace(base, confidence=None)
        object.__setattr__(row, "confidence", "0.85")
        candidate = _candidate_from_agent(row)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.confidence, 0.85)
        self.assertIsInstance(candidate.confidence, float)

    def test_normalized_candidate_numeric_field_types(self) -> None:
        candidate = self._candidate(
            quantity="10",
            strike="25000",
            target="180",
            lot_size="75",
            limit_price="100.5",
            stop_loss="90.5",
            confidence=0.85,
        )
        self.assertIsNotNone(candidate)
        self.assertIsInstance(candidate.quantity, int)
        self.assertIsInstance(candidate.strike, float)
        self.assertIsInstance(candidate.target, float)
        self.assertIsInstance(candidate.lot_size, int)
        self.assertIsInstance(candidate.limit_price, float)
        self.assertIsInstance(candidate.stop_loss, float)
        self.assertIsInstance(candidate.confidence, float)
        self.assertEqual(candidate.quantity, 10)
        self.assertEqual(candidate.strike, 25000.0)
        self.assertEqual(candidate.target, 180.0)
        self.assertEqual(candidate.lot_size, 75)
        self.assertEqual(candidate.limit_price, 100.5)
        self.assertEqual(candidate.stop_loss, 90.5)
        self.assertEqual(candidate.confidence, 0.85)

    def test_malformed_still_fail_closed_after_normalization(self) -> None:
        self.assertIsNone(self._candidate(quantity="abc"))
        self.assertIsNone(self._candidate(quantity=None))
        self.assertIsNone(self._candidate(strike="abc"))
        self.assertIsNone(self._candidate(target="abc"))
        self.assertIsNone(self._candidate(lot_size="abc"))
        self.assertIsNone(self._candidate(limit_price="abc"))
        self.assertIsNone(self._candidate(stop_loss="abc"))
        self.assertIsNone(
            self._candidate(
                quantity="abc",
                strike="abc",
                target="abc",
                lot_size="abc",
                limit_price="abc",
                stop_loss="abc",
            )
        )


class DecisionEngineSignalAttachmentTests(unittest.TestCase):
    def test_decide_attaches_campaign_signal(self) -> None:
        """1. decide() returns campaign_signal != None."""
        snap = _snapshot()
        decision = _engine().decide(snapshot=snap, package=_package(snap, _result(snap)))
        self.assertIsNotNone(decision.campaign_signal)
        self.assertIsNotNone(decision.to_dict()["campaign_signal"])

    def test_decide_buy_ce_signal_action(self) -> None:
        """2. BUY_CE decision has campaign_signal.action == BUY_CE."""
        snap = _snapshot()
        decision = _engine().decide(snapshot=snap, package=_package(snap, _result(snap)))
        self.assertEqual(decision.action, DecisionAction.BUY_CE)
        self.assertEqual(decision.campaign_signal["action"], "BUY_CE")

    def test_decide_buy_pe_signal_action(self) -> None:
        """3. BUY_PE decision has campaign_signal.action == BUY_PE."""
        snap = _snapshot(quotes=(_quote(option_type="PE", provider_contract_id="RELIANCE-2500-PE"),))
        result = _result(
            snap,
            instrument="RELIANCE-2500-PE",
            direction="BEARISH",
            option_type="PE",
            stop_loss=80.0,
            target=60.0,
        )
        decision = _engine().decide(snapshot=snap, package=_package(snap, result))
        self.assertEqual(decision.action, DecisionAction.BUY_PE)
        self.assertEqual(decision.campaign_signal["action"], "BUY_PE")

    def test_replay_reattaches_campaign_signal(self) -> None:
        """4. replay() re-attaches campaign_signal."""
        snap = _snapshot()
        engine = _engine()
        first = engine.decide(snapshot=snap, package=_package(snap, _result(snap)))
        replayed = engine.replay(first.decision_id)
        self.assertIsNotNone(replayed.campaign_signal)
        self.assertEqual(replayed.campaign_signal["action"], first.campaign_signal["action"])
        self.assertEqual(replayed.campaign_signal["signal_id"], first.campaign_signal["signal_id"])
        self.assertEqual(replayed.to_dict()["campaign_signal"], first.to_dict()["campaign_signal"])

    def test_debate_disagreement_decision_remains_no_trade(self) -> None:
        """5. SignalEngine NO_TRADE remains NO_TRADE when debate disagrees."""
        snap = _snapshot()
        decision = _engine().decide(
            snapshot=snap,
            package=_package(
                snap,
                _result(snap),
                agreement=False,
                conflicts=("ACTION_CONFLICT:PAPER_OPEN,HOLD",),
                dissenting=("other_agent",),
            ),
        )
        self.assertEqual(decision.action, DecisionAction.NO_TRADE)
        self.assertEqual(decision.campaign_signal["action"], "NO_TRADE")
        self.assertIn("DEBATE_DISAGREEMENT", decision.campaign_signal["reason_codes"])

    def test_chain_filter_rejection_decision_remains_no_trade(self) -> None:
        """6. Chain-filter rejection remains NO_TRADE."""
        snap = _snapshot(quotes=(_quote(expiry_class=None),))
        decision = _engine().decide(snapshot=snap, package=_package(snap, _result(snap)))
        self.assertEqual(decision.action, DecisionAction.NO_TRADE)
        self.assertEqual(decision.campaign_signal["action"], "NO_TRADE")
        self.assertIn("UNKNOWN_EXPIRY_CLASS", decision.campaign_signal["reason_codes"])

    def test_risk_guard_authoritative_over_buy_signal(self) -> None:
        """7. Risk Guard rejection remains authoritative even when SignalEngine is BUY_CE."""
        snap = _snapshot()
        package = _package(snap, _result(snap))
        # Standalone signal would be BUY_CE.
        signal = SignalEngine(load_config()).evaluate(snapshot=snap, package=package)
        self.assertEqual(signal.action, DecisionAction.BUY_CE)
        decision = _engine().decide(
            snapshot=snap,
            package=package,
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
        # Signal still explains BUY_CE intent; it does not override Risk Guard.
        self.assertIsNotNone(decision.campaign_signal)
        self.assertEqual(decision.campaign_signal["action"], "BUY_CE")

    def test_signal_engine_evaluated_once_per_decide(self) -> None:
        """8. SignalEngine is evaluated exactly once per decide() call."""
        snap = _snapshot()
        package = _package(snap, _result(snap))
        config = load_config()
        engine = DecisionEngine(config, risk_guard=make_guard(config, clock=FrozenClock(AS_OF)))
        with patch.object(engine._signal_engine, "evaluate", wraps=engine._signal_engine.evaluate) as mocked:
            decision = engine.decide(snapshot=snap, package=package)
        self.assertEqual(mocked.call_count, 1)
        self.assertIsNotNone(decision.campaign_signal)

    def test_integrator_invoked_once_per_decide(self) -> None:
        """9. DecisionIntegrator is invoked exactly once per decide() call."""
        snap = _snapshot()
        package = _package(snap, _result(snap))
        config = load_config()
        engine = DecisionEngine(config, risk_guard=make_guard(config, clock=FrozenClock(AS_OF)))
        with patch.object(engine._integrator, "integrate", wraps=engine._integrator.integrate) as mocked:
            decision = engine.decide(snapshot=snap, package=package)
        self.assertEqual(mocked.call_count, 1)
        self.assertIsNotNone(decision.campaign_signal)

    def test_no_broker_or_live_order_path_in_signal_modules(self) -> None:
        """10. No broker/live-order path is introduced."""
        banned = {"place_order", "place_live_order", "kiteconnect", "LiveBroker"}
        paths = [
            ROOT / "grow" / "decision" / "integration" / "engine.py",
            ROOT / "grow" / "decision" / "signal" / "engine.py",
            ROOT / "grow" / "decision" / "signal" / "models.py",
        ]
        for path in paths:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, banned)
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, banned)
            self.assertNotIn("grow.execution.live", source)

    def test_decide_source_has_no_early_return_before_signal(self) -> None:
        """Structural guard: decide() must evaluate signal before returning."""
        source = (ROOT / "grow" / "decision" / "integration" / "engine.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        decide_fn = None
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "DecisionEngine":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "decide":
                        decide_fn = item
        self.assertIsNotNone(decide_fn)
        # First meaningful call should be signal evaluate; must not return integrate alone.
        text = ast.get_source_segment(source, decide_fn) or ""
        self.assertIn("_signal_engine.evaluate", text)
        self.assertIn("_integrator.integrate", text)
        self.assertIn("_with_signal", text)
        # Forbidden early-return pattern: return integrate(...) as the whole body outcome
        # without _with_signal.
        self.assertNotRegex(
            text,
            r"return self\._integrator\.integrate\([^)]*\)\s*$",
        )


if __name__ == "__main__":
    unittest.main()
