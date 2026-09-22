"""Requirement 4C — decision contract, safety gates, audit, and replay."""

from __future__ import annotations

import ast
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.contract import REQUIRED_DECISION_FIELDS, IntegratedDecisionStatus
from grow.decision.integration.integrator import DecisionAuditLog, DecisionIntegrator
from grow.decision.integration.policy import classify_output
from grow.errors import GrowSafetyError
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.orchestration.models import AggregateAnalysisPackage

from tests.helpers import make_guard


ROOT = Path(__file__).resolve().parents[1]
AS_OF = datetime(2026, 9, 22, 11, 0, tzinfo=IST)


def _snapshot(quality: DataQualityStatus = DataQualityStatus.OK, *, options=()):
    return build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=AS_OF,
        spot=100.0,
        option_contracts=options,
        quality=quality,
        notes=("stale",) if quality is DataQualityStatus.STALE else (),
    )


def _metrics(**overrides):
    payload = {
        "strategy": "trend",
        "direction": "BULLISH",
        "underlying": "RELIANCE",
        "limit_price": 100.0,
        "stop_loss": 98.0,
        "quantity": 1,
    }
    payload.update(overrides)
    return payload


def _result(snapshot, *, name="strategy_research", action=CandidateAction.PAPER_OPEN, **kwargs):
    metrics = kwargs.pop("metrics", _metrics())
    return AgentResult(
        agent_name=name,
        agent_version=kwargs.pop("version", f"{name}.v2"),
        snapshot_id=kwargs.pop("snapshot_id", snapshot.snapshot_id),
        snapshot_version=kwargs.pop("snapshot_version", snapshot.version),
        decision_timestamp=snapshot.decision_timestamp,
        status=kwargs.pop("status", AgentStatus.PASS),
        observations=kwargs.pop("observations", ("spot=100",)),
        calculated_metrics=metrics,
        interpretation=kwargs.pop("interpretation", ()),
        findings=kwargs.pop("findings", ("UNANIMOUS_OPEN",)),
        data_quality_concerns=(),
        assumptions=("buyer-only",),
        evidence=(f"snapshot_id={snapshot.snapshot_id}",),
        metrics_used=("limit_price", "stop_loss"),
        candidate_action=action,
        candidate_instrument=kwargs.pop("instrument", "RELIANCE"),
        entry_reason="test",
        invalidation_reason=None,
        risk_flags=kwargs.pop("risk_flags", ()),
        missing_data=kwargs.pop("missing_data", ()),
        confidence=0.4,
        cycle_id=kwargs.pop("cycle_id", "cycle-4c"),
    )


def _debate():
    return DebateSummary(
        agreement=True,
        actions=("PAPER_OPEN",),
        agreeing_agents=("strategy_research",),
        dissenting_agents=(),
        conflicts=(),
        evidence=(),
        insufficient_agents=(),
        error_agents=(),
    )


def _package(snapshot, outputs, **kwargs):
    if isinstance(outputs, AgentResult):
        outputs = (outputs,)
    return AggregateAnalysisPackage(
        cycle_id=kwargs.pop("cycle_id", "cycle-4c"),
        snapshot_id=kwargs.pop("snapshot_id", snapshot.snapshot_id),
        snapshot_version=kwargs.pop("snapshot_version", snapshot.version),
        as_of=snapshot.decision_timestamp,
        agent_outputs=tuple(outputs),
        rejected_outputs=kwargs.pop("rejected_outputs", ()),
        dispatch_records=(),
        conflicts=kwargs.pop("conflicts", ()),
        supporting_evidence=(),
        conflicting_evidence=(),
        unavailable_agents=kwargs.pop("unavailable_agents", ()),
        debate=kwargs.pop("debate", _debate()),
        cycle_summary="test",
        package_digest=kwargs.pop("package_digest", "pkg-fixed"),
        paper_mode=kwargs.pop("paper_mode", True),
        live_trading=kwargs.pop("live_trading", False),
    )


def _integrator(config=None, **risk):
    config = config or load_config()
    if risk:
        config = replace(config, risk=replace(config.risk, **risk))
    guard = make_guard(config, clock=FrozenClock(AS_OF))
    return DecisionIntegrator(config, risk_guard=guard)


class DecisionIntegrationTests(unittest.TestCase):
    def test_decision_contract_schema(self) -> None:
        snap = _snapshot()
        decision = _integrator().integrate(snapshot=snap, package=_package(snap, (_result(snap),)))
        payload = decision.to_dict()
        for key in REQUIRED_DECISION_FIELDS:
            self.assertIn(key, payload)
        self.assertEqual(payload["schema_version"], "grow.decision.integration.v1")
        self.assertIn(payload["status"], {item.value for item in IntegratedDecisionStatus})
        self.assertEqual(payload["status"], "CANDIDATE")
        self.assertTrue(payload["paper_trade_candidate"])
        self.assertFalse(payload["live_trading"])
        self.assertFalse(payload["broker_order_path"])
        self.assertFalse(payload["executed"])
        self.assertTrue(payload["paper_mode"])
        self.assertEqual(payload["analysis_cycle_id"], "cycle-4c")
        self.assertEqual(payload["snapshot_id"], snap.snapshot_id)
        self.assertEqual(payload["snapshot_version"], snap.version)
        self.assertEqual(payload["decision_timestamp"], payload["as_of"])
        self.assertTrue(payload["audit_references"])
        self.assertEqual(payload["risk_guard_result"], "APPROVED")
        self.assertEqual(payload["risk_guard_reason"], "approved")

    def test_snapshot_and_version_mismatches_are_rejected(self) -> None:
        snap = _snapshot()
        bad_id = _result(snap, snapshot_id="other-snapshot")
        bad_version = _result(snap, snapshot_version="other-version")
        bad_cycle = _result(snap, cycle_id="cycle-other")
        for output, token in (
            (bad_id, "SNAPSHOT_ID_MISMATCH"),
            (bad_version, "SNAPSHOT_VERSION_MISMATCH"),
            (bad_cycle, "CYCLE_ID_MISMATCH"),
        ):
            decision = _integrator().integrate(snapshot=snap, package=_package(snap, (output,)))
            self.assertEqual(decision.status, IntegratedDecisionStatus.NO_TRADE)
            self.assertIn(token, decision.reason_codes)
            self.assertTrue(decision.agent_output_refs)
            self.assertFalse(any(row.relied_upon for row in decision.agent_output_refs))
            self.assertEqual(decision.risk_guard_result, "NOT_EVALUATED")

        ok, reason, _ = classify_output("not-a-result", snapshot=snap, cycle_id="cycle-4c")
        self.assertFalse(ok)
        self.assertEqual(reason, "SCHEMA_MISMATCH")
        schema_decision = _integrator().integrate(
            snapshot=snap,
            package=_package(snap, ("not-a-result",)),
        )
        self.assertEqual(schema_decision.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertIn("SCHEMA_MISMATCH", schema_decision.reason_codes)
        self.assertIsNone(schema_decision.candidate_strategy)

        mismatched = _package(snap, (_result(snap),), snapshot_id="wrong-package")
        blocked = _integrator().integrate(snapshot=snap, package=mismatched)
        self.assertEqual(blocked.status, IntegratedDecisionStatus.BLOCKED)
        self.assertIn("SNAPSHOT_MISMATCH", blocked.reason_codes)

    def test_agent_disagreement_is_preserved(self) -> None:
        snap = _snapshot()
        left = _result(snap, name="alpha", instrument="RELIANCE")
        right = _result(snap, name="beta", instrument="TCS")
        decision = _integrator().integrate(snapshot=snap, package=_package(snap, (left, right)))
        self.assertEqual(decision.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertIn("AGENT_CONFLICT", decision.reason_codes)
        self.assertTrue(any(item.startswith("INSTRUMENT_CONFLICT:") for item in decision.conflicting_findings))
        self.assertIsNone(decision.direction)
        self.assertIsNone(decision.candidate_strategy)
        self.assertFalse(decision.paper_trade_candidate)

        forced = _package(
            snap,
            (_result(snap),),
            conflicts=("DIRECTION_CONFLICT:bullish=alpha;bearish=beta",),
        )
        kept = _integrator().integrate(snapshot=snap, package=forced)
        self.assertEqual(kept.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertIn("DIRECTION_CONFLICT:bullish=alpha;bearish=beta", kept.conflicting_findings)

    def test_risk_guard_approves_paper_candidate_without_execution(self) -> None:
        snap = _snapshot()
        decision = _integrator().integrate(snapshot=snap, package=_package(snap, (_result(snap),)))
        self.assertEqual(decision.status, IntegratedDecisionStatus.CANDIDATE)
        self.assertEqual(decision.candidate_strategy, "trend")
        self.assertEqual(decision.candidate_instrument, "RELIANCE")
        self.assertEqual(decision.direction, "BULLISH")
        self.assertFalse(decision.executed)
        self.assertFalse(decision.broker_order_path)
        self.assertIn("PAPER_TRADE_CANDIDATE", decision.reason_codes)

    def test_risk_guard_blocks_despite_consensus(self) -> None:
        snap = _snapshot()
        agents = (
            _result(snap, name="alpha"),
            _result(snap, name="beta"),
        )
        decision = _integrator().integrate(
            snapshot=snap,
            package=_package(snap, agents),
            book=_book(daily_pnl=-20_000),
        )
        self.assertEqual(decision.status, IntegratedDecisionStatus.BLOCKED)
        self.assertFalse(decision.paper_trade_candidate)
        self.assertEqual(decision.risk_guard_result, "REJECTED")
        self.assertIn("loss.daily", decision.risk_guard_reason)
        self.assertIn("RISK_GUARD_REJECTED", decision.reason_codes)

    def test_daily_loss_per_trade_risk_and_position_limit(self) -> None:
        snap = _snapshot()
        package = _package(snap, (_result(snap),))
        daily = _integrator().integrate(snapshot=snap, package=package, book=_book(daily_pnl=-20_000))
        self.assertEqual(daily.status, IntegratedDecisionStatus.BLOCKED)
        self.assertIn("loss.daily", daily.risk_guard_reason)

        per_trade = _integrator(max_per_trade_risk=10.0).integrate(
            snapshot=snap,
            package=_package(snap, (_result(snap, metrics=_metrics(stop_loss=90.0, quantity=2)))),
        )
        self.assertEqual(per_trade.status, IntegratedDecisionStatus.BLOCKED)
        self.assertIn("risk.per_trade", per_trade.risk_guard_reason)

        positions = _integrator(max_open_positions=1).integrate(
            snapshot=snap,
            package=package,
            book=_book(open_positions=1),
        )
        self.assertEqual(positions.status, IntegratedDecisionStatus.BLOCKED)
        self.assertIn("positions.count", positions.risk_guard_reason)

    def test_data_quality_fails_closed(self) -> None:
        stale = _snapshot(DataQualityStatus.STALE)
        stale_decision = _integrator().integrate(
            snapshot=stale,
            package=_package(stale, (_result(stale),)),
        )
        self.assertEqual(stale_decision.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertIn("DATA_STALE", stale_decision.reason_codes)
        self.assertEqual(stale_decision.risk_guard_result, "NOT_EVALUATED")

        rejected = _snapshot(DataQualityStatus.REJECTED)
        rejected_decision = _integrator().integrate(
            snapshot=rejected,
            package=_package(rejected, (_result(rejected),)),
        )
        self.assertEqual(rejected_decision.status, IntegratedDecisionStatus.BLOCKED)
        self.assertIn("DATA_REJECTED", rejected_decision.reason_codes)

        missing = _snapshot(DataQualityStatus.INSUFFICIENT)
        missing_decision = _integrator().integrate(
            snapshot=missing,
            package=_package(missing, ()),
        )
        self.assertEqual(missing_decision.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertIn("DATA_INSUFFICIENT", missing_decision.reason_codes)

        option = OptionQuoteView(
            underlying="RELIANCE",
            expiry=AS_OF.date(),
            strike=25000.0,
            option_type="CE",
            ltp=None,
            bid=1.0,
            ask=2.0,
            open_interest=1,
            volume=1,
            quote_timestamp=AS_OF,
            quote_age_seconds=0.0,
            provider_contract_id="ce-stale",
            quality=DataQualityStatus.STALE,
        )
        quoted = _snapshot(options=(option,))
        stale_option = _integrator().integrate(
            snapshot=quoted,
            package=_package(
                quoted,
                (_result(quoted, instrument="RELIANCE-25000-CE", metrics=_metrics())),
            ),
        )
        self.assertEqual(stale_option.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertIn("DATA_STALE", stale_option.reason_codes)

    def test_no_strategy_candidate_is_no_trade(self) -> None:
        snap = _snapshot()
        decision = _integrator().integrate(
            snapshot=snap,
            package=_package(snap, (_result(snap, action=CandidateAction.NONE, findings=("OBSERVED",)))),
        )
        self.assertEqual(decision.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertIn("NO_VALID_STRATEGY_CANDIDATE", decision.reason_codes)
        self.assertFalse(decision.paper_trade_candidate)

    def test_agents_cannot_change_risk_limits(self) -> None:
        snap = _snapshot()
        config = load_config()
        original = config.risk.max_daily_loss
        metrics = _metrics(max_daily_loss=1_000_000_000, max_per_trade_risk=1_000_000_000)
        decision = _integrator(config).integrate(
            snapshot=snap,
            package=_package(snap, (_result(snap, metrics=metrics))),
            book=_book(daily_pnl=-20_000),
        )
        self.assertEqual(decision.status, IntegratedDecisionStatus.BLOCKED)
        self.assertIn("loss.daily", decision.risk_guard_reason)
        self.assertEqual(config.risk.max_daily_loss, original)

    def test_replay_is_deterministic_and_audit_is_immutable(self) -> None:
        snap = _snapshot()
        package = _package(snap, (_result(snap),))
        first = _integrator()
        second = _integrator()
        left = first.integrate(snapshot=snap, package=package)
        right = second.integrate(snapshot=snap, package=package)
        self.assertEqual(left.to_dict(), right.to_dict())
        replayed = first.replay(left.decision_id)
        self.assertEqual(replayed.to_dict(), left.to_dict())
        self.assertEqual(len(first.audit), 1)
        again = first.integrate(snapshot=snap, package=package)
        self.assertEqual(again.decision_id, left.decision_id)
        self.assertEqual(len(first.audit), 1)

        blocked = first.integrate(snapshot=snap, package=package, book=_book(daily_pnl=-20_000))
        self.assertEqual(blocked.status, IntegratedDecisionStatus.BLOCKED)
        self.assertEqual(len(first.audit), 2)
        self.assertEqual(first.audit.decision_ids()[0], left.decision_id)
        self.assertEqual(first.replay(blocked.decision_id).to_dict(), blocked.to_dict())
        self.assertTrue(blocked.reason_codes)
        self.assertTrue(blocked.audit_references)
        self.assertEqual(left.risk_guard_result, "APPROVED")
        self.assertEqual(blocked.risk_guard_result, "REJECTED")

        tampered = replace(left, reason_codes=("TAMPERED",))
        with self.assertRaises(GrowSafetyError):
            first.audit.append(tampered, snapshot=snap, package=package, book=_book())
        self.assertEqual(first.audit.get(left.decision_id).decision.reason_codes, left.reason_codes)
        self.assertEqual(len(first.audit), 2)

    def test_no_trade_and_blocked_are_fully_recorded(self) -> None:
        snap = _snapshot()
        log = DecisionAuditLog()
        integrator = _integrator()
        integrator.audit = log
        no_trade = integrator.integrate(
            snapshot=snap,
            package=_package(snap, (_result(snap, action=CandidateAction.ABSTAIN),)),
        )
        blocked = integrator.integrate(
            snapshot=snap,
            package=_package(snap, (_result(snap),)),
            book=_book(daily_pnl=-20_000),
        )
        self.assertEqual(len(log), 2)
        for decision in (no_trade, blocked):
            stored = log.get(decision.decision_id).decision
            self.assertEqual(stored.to_dict(), decision.to_dict())
            self.assertTrue(stored.reason_codes)
            self.assertTrue(stored.audit_references)
            self.assertEqual(stored.snapshot_id, snap.snapshot_id)
            self.assertEqual(stored.analysis_cycle_id, "cycle-4c")
            self.assertFalse(stored.executed)

    def test_no_broker_call_path(self) -> None:
        root = ROOT / "grow" / "decision" / "integration"
        banned_modules = {
            "grow.execution.live",
            "grow.paper.ledger",
            "kiteconnect",
            "grow.execution.live.LiveBroker",
        }
        banned_calls = {"place_order", "place_live_order", "submit"}
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name, banned_modules)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    self.assertFalse(module.startswith("grow.execution.live"))
                    self.assertNotEqual(module, "grow.paper.ledger")
                    self.assertNotIn(module, {"kiteconnect", "upstox", "dhan"})
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, banned_calls)
        decision = _integrator().integrate(
            snapshot=_snapshot(),
            package=_package(_snapshot(), (_result(_snapshot()),)),
        )
        self.assertFalse(decision.to_dict()["broker_order_path"])
        self.assertFalse(decision.executed)


def _book(**kwargs):
    from grow.decision.integration.contract import DecisionBookState

    defaults = dict(cash=1_000_000.0, gross_notional=0.0, daily_pnl=0.0, symbol_notional=0.0, open_positions=0)
    defaults.update(kwargs)
    return DecisionBookState(**defaults)


if __name__ == "__main__":
    unittest.main()
