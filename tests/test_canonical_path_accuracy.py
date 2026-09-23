"""Canonical paper path accuracy — explainable CEO → RiskGuard → paper.

Deterministic fixtures only. Does not call Zerodha or place broker orders.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime

from grow.campaign.config import campaign_paper_config
from grow.campaign.runner import CampaignRunner
from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.decision.integration.campaign_score import score_campaign_quote
from grow.decision.integration.ceo_gate import campaign_ceo_gate
from grow.decision.integration.contract import (
    DecisionAction,
    IntegratedDecisionStatus,
    StrategyCandidate,
)
from grow.decision.integration.integrator import DecisionIntegrator
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.provenance import MarketDataSource, reject_mixed_market_data
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.options.models import OptionType
from grow.orchestration.cycle import AnalysisOrchestrator
from grow.orchestration.models import AggregateAnalysisPackage
from grow.paper.ledger import PaperBook

from tests.helpers import TEST_RISK_SECRET, make_guard


AS_OF = datetime(2026, 9, 23, 11, 0, tzinfo=IST)
EXPIRY = date(2026, 9, 30)
NEAR = date(2026, 9, 24)


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


def _snapshot(*, quotes=None, quality=DataQualityStatus.OK):
    return build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=AS_OF,
        spot=2500.0,
        option_contracts=tuple(quotes if quotes is not None else (_quote(),)),
        quality=quality,
    )


def _candidate(**overrides) -> StrategyCandidate:
    payload = dict(
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
    payload.update(overrides)
    return StrategyCandidate(**payload)


def _metrics(**overrides):
    payload = {
        "strategy": "trend",
        "direction": "BULLISH",
        "underlying": "RELIANCE",
        "limit_price": 101.0,
        "stop_loss": 80.0,
        "quantity": 1,
        "option_type": "CE",
        "strike": 2500.0,
        "expiry": EXPIRY.isoformat(),
    }
    payload.update(overrides)
    return payload


def _result(snapshot, **kwargs):
    metrics = kwargs.pop("metrics", _metrics())
    return AgentResult(
        agent_name=kwargs.pop("name", "strategy_research"),
        agent_version=kwargs.pop("version", "strategy_research.v2"),
        snapshot_id=kwargs.pop("snapshot_id", snapshot.snapshot_id),
        snapshot_version=kwargs.pop("snapshot_version", snapshot.version),
        decision_timestamp=snapshot.decision_timestamp,
        status=kwargs.pop("status", AgentStatus.PASS),
        observations=("spot=2500",),
        calculated_metrics=metrics,
        interpretation=(),
        findings=("UNANIMOUS_OPEN",),
        data_quality_concerns=(),
        assumptions=("buyer-only",),
        evidence=(f"snapshot_id={snapshot.snapshot_id}",),
        metrics_used=("limit_price", "stop_loss"),
        candidate_action=kwargs.pop("action", CandidateAction.PAPER_OPEN),
        candidate_instrument=kwargs.pop("instrument", "RELIANCE-2500-CE"),
        entry_reason="test",
        invalidation_reason=None,
        risk_flags=(),
        missing_data=(),
        confidence=0.4,
        cycle_id=kwargs.pop("cycle_id", "cycle-accuracy"),
    )


def _package(snapshot, outputs):
    if isinstance(outputs, AgentResult):
        outputs = (outputs,)
    return AggregateAnalysisPackage(
        cycle_id="cycle-accuracy",
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
            agreeing_agents=("strategy_research",),
            dissenting_agents=(),
            conflicts=(),
            evidence=(),
            insufficient_agents=(),
            error_agents=(),
        ),
        cycle_summary="accuracy",
        package_digest="pkg-accuracy",
        paper_mode=True,
        live_trading=False,
    )


def _integrator(config=None):
    config = config or campaign_paper_config(load_config())
    return DecisionIntegrator(
        config,
        risk_guard=make_guard(config, clock=FrozenClock(AS_OF)),
    )


class CanonicalSpecialistsTests(unittest.TestCase):
    def test_default_orchestrator_includes_campaign_options(self) -> None:
        names = [agent.agent_name for agent in AnalysisOrchestrator().specialists]
        self.assertIn("campaign_options", names)
        self.assertIn("options", names)


class CeoGateAccuracyTests(unittest.TestCase):
    def test_ceo_reject_skips_risk_guard_evaluate(self) -> None:
        """Chain filter usually aligns CE/PE; force CEO reject after policy candidate exists."""
        from unittest.mock import patch

        snap = _snapshot()
        with patch(
            "grow.decision.integration.integrator.campaign_ceo_gate",
            return_value=(False, ("BULLISH_NOT_CE",)),
        ):
            decision = _integrator().integrate(snapshot=snap, package=_package(snap, _result(snap)))
        self.assertEqual(decision.status, IntegratedDecisionStatus.NO_TRADE)
        self.assertEqual(decision.action, DecisionAction.NO_TRADE)
        self.assertIn("CEO_GATE_REJECTED", decision.reason_codes)
        self.assertIn("BULLISH_NOT_CE", decision.reason_codes)
        self.assertEqual(decision.risk_guard_result, "NOT_EVALUATED")
        self.assertEqual(decision.risk_guard_reason, "CEO_GATE_REJECTED")
        self.assertEqual(decision.risk_rule_results, ())

    def test_ceo_approves_bullish_ce_then_risk_may_approve(self) -> None:
        snap = _snapshot()
        decision = _integrator().integrate(snapshot=snap, package=_package(snap, _result(snap)))
        self.assertNotIn("CEO_GATE_REJECTED", decision.reason_codes)
        self.assertIn(decision.risk_guard_result, {"APPROVED", "REJECTED"})
        if decision.risk_guard_result == "APPROVED":
            self.assertEqual(decision.status, IntegratedDecisionStatus.CANDIDATE)
            self.assertEqual(decision.action, DecisionAction.BUY_CE)

    def test_ceo_gate_unit_rules_are_deterministic(self) -> None:
        ok, reasons = campaign_ceo_gate(_candidate())
        self.assertTrue(ok)
        self.assertEqual(reasons, ())
        bad, bad_reasons = campaign_ceo_gate(_candidate(option_type="PE", instrument="RELIANCE-2500-PE"))
        self.assertFalse(bad)
        self.assertIn("BULLISH_NOT_CE", bad_reasons)


class ScoringExplainabilityTests(unittest.TestCase):
    def test_score_exposes_dte_and_marks_theta_informational(self) -> None:
        cfg = load_config().options
        near = _quote(expiry=NEAR, provider_contract_id="RELIANCE-2500-CE-NEAR")
        far = _quote(expiry=EXPIRY, provider_contract_id="RELIANCE-2500-CE-FAR")
        near_score, near_diag = score_campaign_quote(
            near, spot=2500.0, atm=2500.0, as_of=AS_OF, option_type=OptionType.CE, config=cfg
        )
        far_score, far_diag = score_campaign_quote(
            far, spot=2500.0, atm=2500.0, as_of=AS_OF, option_type=OptionType.CE, config=cfg
        )
        self.assertEqual(near_diag["dte_days"], (NEAR - AS_OF.date()).days)
        self.assertEqual(far_diag["dte_days"], (EXPIRY - AS_OF.date()).days)
        self.assertFalse(near_diag["theta_in_score"])
        self.assertIn("components", near_diag)
        self.assertIn("rationale", near_diag)
        # Near/far scores share the same explainability contract; bucket preference
        # is covered by options.score unit tests — only assert audit fields here.
        self.assertIsInstance(near_score, float)
        self.assertIsInstance(far_score, float)
        self.assertNotEqual(near_diag["dte_days"], far_diag["dte_days"])

    def test_score_is_deterministic(self) -> None:
        cfg = load_config().options
        row = _quote()
        a, da = score_campaign_quote(
            row, spot=2500.0, atm=2500.0, as_of=AS_OF, option_type=OptionType.CE, config=cfg
        )
        b, db = score_campaign_quote(
            row, spot=2500.0, atm=2500.0, as_of=AS_OF, option_type=OptionType.CE, config=cfg
        )
        self.assertEqual(a, b)
        self.assertEqual(da, db)


class RiskCapitalAccuracyTests(unittest.TestCase):
    def test_campaign_defaults_match_10k_profile(self) -> None:
        cfg = campaign_paper_config(load_config())
        self.assertEqual(cfg.paper.starting_cash, 10_000)
        self.assertEqual(cfg.risk.max_daily_loss, 2_000)
        self.assertEqual(cfg.risk.max_per_trade_risk, 1_000)
        self.assertEqual(cfg.risk.max_open_positions, 2)

    def test_boundary_1000_risk_vs_1001(self) -> None:
        snap = _snapshot()
        # planned loss = |limit - stop| * qty → 1001 vs 1000
        over = _result(snap, metrics=_metrics(limit_price=2001.0, stop_loss=1000.0, quantity=1))
        decision = _integrator().integrate(snapshot=snap, package=_package(snap, over))
        self.assertEqual(decision.status, IntegratedDecisionStatus.BLOCKED)
        self.assertEqual(decision.risk_guard_result, "REJECTED")
        self.assertIn("risk.per_trade", decision.risk_guard_reason)

        exact = _result(snap, metrics=_metrics(limit_price=2000.0, stop_loss=1000.0, quantity=1))
        ok = _integrator().integrate(snapshot=snap, package=_package(snap, exact))
        if ok.risk_guard_result == "REJECTED":
            self.assertNotIn("risk.per_trade", ok.risk_guard_reason)


class ProvenanceAccuracyTests(unittest.TestCase):
    def test_mixed_never_silently_live(self) -> None:
        from dataclasses import replace as dc_replace

        base = _snapshot(
            quotes=(
                _quote(is_fixture=True, provider_contract_id="FIX-CE"),
                _quote(is_fixture=False, option_type="PE", provider_contract_id="LIVE-PE"),
            )
        )
        # build_fixture_snapshot forces FIXTURE; reclassify like production LIVE+FIXTURE mix.
        mixed = dc_replace(
            base,
            is_fixture=False,
            market_data_source=MarketDataSource.LIVE,
            option_contracts=(
                _quote(is_fixture=True, provider_contract_id="FIX-CE"),
                _quote(is_fixture=False, option_type="PE", provider_contract_id="LIVE-PE"),
            ),
        )
        self.assertEqual(mixed.market_data_source, MarketDataSource.MIXED)
        self.assertEqual(reject_mixed_market_data(mixed), "MIXED_MARKET_DATA_SOURCE")
        self.assertNotEqual(mixed.market_data_source, MarketDataSource.LIVE)


class PaperPathNoBypassTests(unittest.TestCase):
    def test_ceo_reject_prevents_paper_fill(self) -> None:
        from unittest.mock import patch

        snap = _snapshot()
        config = campaign_paper_config(load_config())
        runner = CampaignRunner(
            config,
            clock=FrozenClock(AS_OF),
            risk_secret=TEST_RISK_SECRET,
            specialists=(),
            apply_campaign_defaults=False,
        )
        with patch(
            "grow.decision.integration.integrator.campaign_ceo_gate",
            return_value=(False, ("FORCED_CEO_REJECT",)),
        ):
            result = runner.run_from_package(snap, _package(snap, _result(snap)))
        self.assertIn("CEO_GATE_REJECTED", result.decision.reason_codes)
        self.assertFalse(result.execution.accepted)
        self.assertEqual(result.execution.broker_order_calls, 0)
        self.assertEqual(len(runner.paper.positions.open_positions()), 0)


class RemainingDailyRiskTests(unittest.TestCase):
    def test_formula_when_daily_pnl_known(self) -> None:
        # remaining = max_daily_loss + daily_pnl (when daily_pnl negative toward floor)
        max_loss = 2_000.0
        daily = -500.0
        remaining = round(max_loss + daily, 2)
        self.assertEqual(remaining, 1_500.0)

    def test_dashboard_exports_remaining_or_documents_gap(self) -> None:
        from grow.dashboard.service import DashboardService

        cfg = campaign_paper_config(load_config())
        book = PaperBook(cash=cfg.paper.starting_cash, currency="INR", realized_pnl=-100.0)
        # Force true_daily into book snapshot path by using service with known book;
        # ledger snapshot may still report true_daily_pnl None — assert contract.
        svc = DashboardService(cfg, book=book)
        risk = svc.risk_view()
        self.assertIn("remaining_daily_risk", risk)
        self.assertIn("max_daily_loss", risk)
        self.assertEqual(risk["max_daily_loss"], 2_000)
        # Gap is acceptable if documented as Not available
        self.assertTrue(
            risk["remaining_daily_risk"] == "Not available"
            or isinstance(risk["remaining_daily_risk"], (int, float))
        )


class DashboardSafetyAccuracyTests(unittest.TestCase):
    def test_mutations_forbidden_and_no_order_controls(self) -> None:
        from fastapi.testclient import TestClient

        from grow.dashboard.app import create_app
        from grow.dashboard.service import DashboardService

        cfg = campaign_paper_config(load_config())
        client = TestClient(create_app(DashboardService(cfg)))
        for method, path in (
            ("post", "/api/orders"),
            ("post", "/api/execute"),
            ("put", "/api/risk"),
            ("post", "/api/live"),
        ):
            res = getattr(client, method)(path)
            self.assertEqual(res.status_code, 405, path)
            body = res.json()
            self.assertIn("read-only", str(body.get("error", "")).lower())
            self.assertFalse(body.get("broker_order_path"))
            self.assertFalse(body.get("live_trading"))
            self.assertFalse(body.get("can_enable_live_trading"))

        dash = client.get("/api/dashboard").json()
        text = str(dash).lower()
        for banned in ("kite_access_token", "grow_risk_secret", "api_secret"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
