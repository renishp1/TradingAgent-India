"""Phase 12 — real historical options validation (PIT walk-forward labeling)."""

from __future__ import annotations

import ast
import inspect
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from grow.backtest.calendar import weekday_sessions
from grow.clock import IST
from grow.config import load_config
from grow.decision.aggregation.debate import DebateSummary
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.errors import GrowConfigError
from grow.history.models import (
    APPROVED,
    APPROVED_FOR_2E,
    FRAMEWORK_TEST_ONLY,
    HISTORICAL_RESEARCH,
    NORM,
    QUALIFIED,
    SCHEMA,
    SYNTHETIC,
    DatasetVersion,
    HistoricalBar,
    HistoricalOptionContract,
    HistoricalOptionQuote,
    HistoricalSession,
)
from grow.history.sample import build_sample_store
from grow.history.store import CanonicalStore
from grow.market_data.normalized.models import DataQualityStatus
from grow.orchestration.models import AggregateAnalysisPackage
from grow.validation.availability import assert_historical_not_today
from grow.validation.historical import (
    HistoricalValidationCampaign,
    build_historical_agent_snapshot,
    build_historical_cycles,
    listed_contracts_from_store,
)
from grow.validation.labels import (
    EvaluationLabel,
    assert_no_fixture_profitability_claim,
    assert_single_evaluation_label,
    classify_store_evaluation_label,
    require_historical_evaluation_dataset,
)
from grow.validation.metrics import calculate_metrics
from grow.validation.replay import HistoricalCycle

from tests.helpers import TEST_RISK_SECRET


ROOT = Path(__file__).resolve().parents[1]
START = date(2026, 9, 1)
OPEN_T = time(9, 15)
CLOSE_T = time(15, 30)
SOURCE = "grow.history.phase12.test"


def _weekdays(start: date, n: int) -> list[date]:
    days: list[date] = []
    cur = start
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _config(**kwargs):
    config = load_config()
    config = replace(
        config,
        live_data=replace(config.live_data, session_timeout_seconds=86400 * 30),
        backtest=replace(
            config.backtest,
            train_sessions=3,
            validate_sessions=1,
            test_sessions=1,
            step_sessions=1,
            embargo_sessions=1,
            calibrate_on_test=False,
        ),
        strategies=replace(config.strategies, universe=("NIFTY", "BANKNIFTY")),
    )
    if kwargs:
        config = replace(config, **kwargs)
    return config


def _build_historical_store(*, sessions: int = 10, publish: bool = True) -> CanonicalStore:
    days = _weekdays(START, sessions)
    version = "2026.09.phase12.hist.a"
    meta = DatasetVersion(
        dataset_id="grow.history.nifty.pit.v1",
        version=version,
        source_version="phase12-raw-1",
        normalization_version=NORM,
        schema_version=SCHEMA,
        calendar_version="nse.session.phase12.v1",
        coverage_start=days[0],
        coverage_end=days[-1],
        fingerprint="pending",
        published_at="2026-09-01T08:00:00+05:30",
        quality_status=APPROVED,
        license_status="APPROVED",
        source_id=SOURCE,
        provider_name="licensed.nse_fo.pit.v1",
        granularity=("M15", "D1"),
        instrument_scope=("NIFTY", "BANKNIFTY"),
        bid_ask_available=True,
        oi_available=True,
        volume_available=True,
        iv_available=False,
        greeks_available=False,
        contract_metadata_available=True,
        option_depth="atm_pm1",
        provenance="HISTORICAL RESEARCH licensed PIT — Phase 12 validation",
        timezone="Asia/Kolkata",
        usage_scope=HISTORICAL_RESEARCH,
        is_fixture=False,
        snapshot_cadence=("11:00",),
        quality_warnings=(),
        mapping_policy="EXACT",
        slot_tolerance_seconds=0,
        qualification_status=APPROVED_FOR_2E,
    )
    store = CanonicalStore(meta)
    expiry = days[-1] + timedelta(days=(1 - days[-1].weekday()) % 7 or 7)
    for day in days:
        open_at = datetime.combine(day, OPEN_T, tzinfo=IST)
        close_at = datetime.combine(day, CLOSE_T, tzinfo=IST)
        store.add_session(
            HistoricalSession(
                session_date=day,
                open_at=open_at,
                close_at=close_at,
                source=SOURCE,
                calendar_version=meta.calendar_version,
                status="OPEN",
            )
        )
        for symbol, spot0, lot, step in (
            ("NIFTY", 25000.0, 75, 50.0),
            ("BANKNIFTY", 52000.0, 15, 100.0),
        ):
            px = spot0 + day.day
            # One M15 bar ending at/before 11:00 so HistoricalMarketSource can price.
            bar_start = datetime.combine(day, time(10, 45), tzinfo=IST)
            bar_end = datetime.combine(day, time(11, 0), tzinfo=IST)
            store.add_bar(
                HistoricalBar(
                    symbol=symbol,
                    timeframe="M15",
                    timestamp=bar_start,
                    end=bar_end,
                    open=px,
                    high=px + 5,
                    low=px - 5,
                    close=px + 1,
                    volume=1000,
                    source_id=SOURCE,
                    dataset_version=version,
                    as_of_available_at=bar_end,
                    corporate_action_adjustment_version="unadjusted.v1",
                    quality_flags=(),
                )
            )
            store.add_bar(
                HistoricalBar(
                    symbol=symbol,
                    timeframe="D1",
                    timestamp=open_at,
                    end=close_at,
                    open=px,
                    high=px + 20,
                    low=px - 20,
                    close=px + 5,
                    volume=100000,
                    source_id=SOURCE,
                    dataset_version=version,
                    as_of_available_at=close_at,
                    corporate_action_adjustment_version="unadjusted.v1",
                    quality_flags=(),
                )
            )
            first = open_at
            last = datetime.combine(expiry, CLOSE_T, tzinfo=IST)
            for k in (-1, 0, 1):
                strike = spot0 + k * step
                for kind in ("CE", "PE"):
                    cid = f"{symbol}-{expiry.isoformat()}-{int(strike)}-{kind}"
                    if not store.has_contract(cid):
                        store.add_contract(
                            HistoricalOptionContract(
                                underlying=symbol,
                                expiry=expiry,
                                strike=strike,
                                option_type=kind,
                                contract_id=cid,
                                provider_contract_id=cid,
                                lot_size=lot,
                                expiry_class="WEEKLY",
                                first_seen_at=first,
                                last_seen_at=last,
                                listing_status="LISTED",
                                source_id=SOURCE,
                                dataset_version=version,
                            )
                        )
                    quote_at = datetime.combine(day, time(11, 0), tzinfo=IST)
                    store.add_quote(
                        HistoricalOptionQuote(
                            contract_id=cid,
                            timestamp=quote_at,
                            bid=100.0 + k,
                            ask=102.0 + k,
                            ltp=101.0 + k,
                            volume=50,
                            open_interest=200,
                            previous_open_interest=190,
                            implied_volatility=None,
                            delta=None,
                            gamma=None,
                            theta=None,
                            vega=None,
                            greek_source=None,
                            iv_source=None,
                            source_id=SOURCE,
                            dataset_version=version,
                            as_of_available_at=quote_at,
                            quality_flags=(),
                        )
                    )
    if publish:
        store.publish()
    return store


def _qualification(store: CanonicalStore, *, status: str = APPROVED_FOR_2E) -> dict:
    return {
        "dataset_id": store.meta.dataset_id,
        "dataset_version": store.meta.version,
        "fingerprint": store.meta.fingerprint,
        "qualification_status": status,
        "approved_for_2e": status == APPROVED_FOR_2E,
        "evaluation_schema": "provider.eval.v1",
        "checks": ("PIT:PASS",),
    }


def _package_for(snapshot, *, cycle_id: str, instrument: str, action=CandidateAction.ABSTAIN):
    output = AgentResult(
        agent_name="strategy_research",
        agent_version="strategy_research.v2",
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        decision_timestamp=snapshot.decision_timestamp,
        status=AgentStatus.PASS,
        observations=("phase12",),
        calculated_metrics={
            "strategy": "trend",
            "direction": "BULLISH",
            "underlying": "NIFTY",
            "limit_price": 101.0,
            "stop_loss": 80.0,
            "quantity": 75,
        },
        interpretation=(),
        findings=("NO_SETUP" if action is CandidateAction.ABSTAIN else "UNANIMOUS_OPEN",),
        data_quality_concerns=(),
        assumptions=("buyer-only",),
        evidence=(f"snapshot_id={snapshot.snapshot_id}",),
        metrics_used=("limit_price", "stop_loss", "quantity"),
        candidate_action=action,
        candidate_instrument=None if action is CandidateAction.ABSTAIN else instrument,
        entry_reason="phase12",
        invalidation_reason=None,
        risk_flags=(),
        missing_data=(),
        confidence=0.2,
        cycle_id=cycle_id,
    )
    return AggregateAnalysisPackage(
        cycle_id=cycle_id,
        snapshot_id=snapshot.snapshot_id,
        snapshot_version=snapshot.version,
        as_of=snapshot.decision_timestamp,
        agent_outputs=(output,),
        rejected_outputs=(),
        dispatch_records=(),
        conflicts=(),
        supporting_evidence=(),
        conflicting_evidence=(),
        unavailable_agents=(),
        debate=DebateSummary(
            agreement=True,
            actions=(action.value,),
            agreeing_agents=(output.agent_name,),
            dissenting_agents=(),
            conflicts=(),
            evidence=(),
            insufficient_agents=(),
            error_agents=(),
        ),
        cycle_summary="phase12",
        package_digest=f"pkg-{cycle_id}",
    )


def _packages_for_cycles(cycles: tuple[HistoricalCycle, ...]) -> dict[str, AggregateAnalysisPackage]:
    packages: dict[str, AggregateAnalysisPackage] = {}
    for i, cycle in enumerate(cycles):
        instrument = cycle.snapshot.option_contracts[0].provider_contract_id
        day_key = cycle.day.isoformat()
        packages[day_key] = _package_for(
            cycle.snapshot,
            cycle_id=f"p12-{i}",
            instrument=instrument,
            action=CandidateAction.ABSTAIN,
        )
    return packages


class EvaluationLabelTests(unittest.TestCase):
    def test_labels_are_distinct_and_unmixed(self) -> None:
        self.assertEqual(
            assert_single_evaluation_label(["HISTORICAL", "HISTORICAL"]),
            EvaluationLabel.HISTORICAL,
        )
        with self.assertRaises(GrowConfigError) as ctx:
            assert_single_evaluation_label(["FIXTURE", "HISTORICAL"])
        self.assertIn("EVALUATION_LABEL_MIXED", str(ctx.exception))

    def test_fixture_profitability_claim_forbidden(self) -> None:
        with self.assertRaises(GrowConfigError) as ctx:
            assert_no_fixture_profitability_claim("FIXTURE", profitability_claim=True)
        self.assertIn("FIXTURE_PROFITABILITY_CLAIM_FORBIDDEN", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            assert_no_fixture_profitability_claim("SYNTHETIC", profitability_claim=True)
        self.assertIn("FIXTURE_PROFITABILITY_CLAIM_FORBIDDEN", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            assert_no_fixture_profitability_claim("HISTORICAL", profitability_claim=True)
        self.assertIn("PROFITABILITY_CLAIM_FORBIDDEN", str(ctx.exception))

    def test_sample_store_is_synthetic_not_historical(self) -> None:
        store = build_sample_store()
        label = classify_store_evaluation_label(store)
        self.assertIn(label, {EvaluationLabel.FIXTURE, EvaluationLabel.SYNTHETIC})
        with self.assertRaises(GrowConfigError) as ctx:
            require_historical_evaluation_dataset(store)
        self.assertIn("FIXTURE_AS_HISTORICAL_FORBIDDEN", str(ctx.exception))


class HistoricalPitCampaignTests(unittest.TestCase):
    def test_fixture_cannot_be_labeled_historical(self) -> None:
        store = build_sample_store()
        with self.assertRaises(GrowConfigError) as ctx:
            HistoricalValidationCampaign(
                store,
                risk_secret=TEST_RISK_SECRET,
                config=_config(),
                evaluation_label="HISTORICAL",
                apply_campaign_profile=True,
            )
        text = str(ctx.exception)
        self.assertTrue(
            "EVALUATION_LABEL_MIXED" in text or "FIXTURE_AS_HISTORICAL_FORBIDDEN" in text
        )

    def test_historical_store_requires_qualification(self) -> None:
        store = _build_historical_store()
        with self.assertRaises(GrowConfigError) as ctx:
            require_historical_evaluation_dataset(
                store,
                {"dataset_id": store.meta.dataset_id, "dataset_version": store.meta.version, "fingerprint": "wrong", "qualification_status": APPROVED_FOR_2E},
            )
        self.assertIn("QUALIFICATION_FINGERPRINT_MISMATCH", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            require_historical_evaluation_dataset(
                store,
                {
                    **_qualification(store, status="CANDIDATE"),
                    "qualification_status": "CANDIDATE",
                },
            )
        self.assertIn("HISTORICAL_DATASET_NOT_READY", str(ctx.exception))

    def test_pit_snapshot_uses_historical_contract_master_only(self) -> None:
        store = _build_historical_store(sessions=5)
        as_of = datetime.combine(_weekdays(START, 1)[0], time(11, 0), tzinfo=IST)
        snap = build_historical_agent_snapshot(store, underlying="NIFTY", as_of=as_of)
        self.assertFalse(snap.is_fixture)
        self.assertTrue(snap.option_contracts)
        self.assertTrue(all(q.quote_timestamp <= as_of for q in snap.option_contracts))
        self.assertTrue(all(q.lot_size and q.lot_size > 0 for q in snap.option_contracts))
        listed = listed_contracts_from_store(store)
        for quote in snap.option_contracts:
            self.assertIn(quote.provider_contract_id, listed)
        # Today's unrelated chain must not reconstruct the historical decision.
        with self.assertRaises(GrowConfigError) as ctx:
            assert_historical_not_today(
                {q.provider_contract_id for q in snap.option_contracts},
                {"TODAY-FAKE-CE", "TODAY-FAKE-PE"},
            )
        self.assertIn("TODAY_CHAIN_RECONSTRUCTION_FORBIDDEN", str(ctx.exception))

    def test_lookahead_quote_rejected_at_snapshot_boundary(self) -> None:
        store = _build_historical_store(sessions=3, publish=False)
        day = _weekdays(START, 1)[0]
        future = datetime.combine(day, time(12, 0), tzinfo=IST)
        # Inject a future-dated quote against an existing contract.
        contract = next(c for c in store.all_contracts() if c.underlying == "NIFTY")
        store.add_quote(
            HistoricalOptionQuote(
                contract_id=contract.contract_id,
                timestamp=future,
                bid=90.0,
                ask=92.0,
                ltp=91.0,
                volume=1,
                open_interest=1,
                previous_open_interest=1,
                implied_volatility=None,
                delta=None,
                gamma=None,
                theta=None,
                vega=None,
                greek_source=None,
                iv_source=None,
                source_id=SOURCE,
                dataset_version=store.meta.version,
                as_of_available_at=future,
                quality_flags=(),
            )
        )
        store.publish()
        as_of = datetime.combine(day, time(11, 0), tzinfo=IST)
        snap = build_historical_agent_snapshot(store, underlying="NIFTY", as_of=as_of)
        self.assertTrue(all(q.quote_timestamp <= as_of for q in snap.option_contracts))

    def test_end_to_end_historical_walk_forward_labeled(self) -> None:
        store = _build_historical_store(sessions=10)
        qual = _qualification(store)
        campaign = HistoricalValidationCampaign(
            store,
            risk_secret=TEST_RISK_SECRET,
            config=_config(),
            qualification=qual,
            evaluation_label="HISTORICAL",
            underlying="NIFTY",
        )
        self.assertEqual(campaign.evaluation_label, EvaluationLabel.HISTORICAL)
        cycles = build_historical_cycles(store, underlying="NIFTY")
        self.assertGreaterEqual(len(cycles), 7)
        packages = _packages_for_cycles(cycles)
        result = campaign.run(packages=packages, include_fee_stress=False)
        payload = result.to_dict()
        self.assertEqual(payload["evaluation_label"], "HISTORICAL")
        self.assertFalse(payload["profitability_claim"])
        self.assertFalse(payload["live"])
        self.assertEqual(payload["today_chain_guard"], "NOT_APPLICABLE")
        self.assertEqual(result.walk_forward.evaluation_label, "HISTORICAL")
        self.assertGreaterEqual(len(result.walk_forward.windows), 1)
        self.assertIn(result.walk_forward.leakage_status, {"CLEAN", "UNKNOWN", "LEAKAGE"})
        for window in result.walk_forward.windows:
            self.assertLess(window.train[1], window.validate[0])
            self.assertLess(window.validate[1], window.test[0])

    def test_today_chain_reconstruction_forbidden(self) -> None:
        store = _build_historical_store(sessions=5)
        with self.assertRaises(GrowConfigError) as ctx:
            HistoricalValidationCampaign(
                store,
                risk_secret=TEST_RISK_SECRET,
                config=_config(),
                qualification=_qualification(store),
                evaluation_label="HISTORICAL",
                underlying="NIFTY",
                today_universe={"NIFTY-2099-01-01-99999-CE"},
            )
        self.assertIn("TODAY_CHAIN_RECONSTRUCTION_FORBIDDEN", str(ctx.exception))

    def test_synthetic_campaign_never_claims_profitability(self) -> None:
        store = build_sample_store()
        campaign = HistoricalValidationCampaign(
            store,
            risk_secret=TEST_RISK_SECRET,
            config=_config(),
            underlying="NIFTY",
            apply_campaign_profile=True,
        )
        self.assertIn(
            campaign.evaluation_label,
            {EvaluationLabel.FIXTURE, EvaluationLabel.SYNTHETIC},
        )
        cycles = build_historical_cycles(store, underlying="NIFTY")
        # Restrict sessions so walk-forward window sizes fit the sample calendar.
        sessions = tuple(sorted({c.day for c in cycles}))[:8]
        packages = _packages_for_cycles(tuple(c for c in cycles if c.day in set(sessions)))
        result = campaign.run(packages=packages, sessions=sessions, include_fee_stress=False)
        self.assertFalse(result.to_dict()["profitability_claim"])
        self.assertNotEqual(result.evaluation_label, EvaluationLabel.HISTORICAL)

    def test_metrics_include_phase12_risk_fields(self) -> None:
        metrics = calculate_metrics(
            trades=[
                {"net_pnl": 10.0, "gross_pnl": 12.0, "total_cost": 2.0, "spread_cost": 1.0, "slippage": 0.5},
                {"net_pnl": -4.0, "gross_pnl": -3.0, "total_cost": 1.0, "spread_cost": 0.5, "slippage": 0.25},
            ],
            decisions=[{"status": "FILLED"}, {"status": "FILLED"}, {"status": "NO_TRADE"}],
            equity=[10000.0, 10010.0, 10006.0],
            starting_cash=10000.0,
        )
        self.assertEqual(metrics["win_rate"], 0.5)
        self.assertEqual(metrics["loss_rate"], 0.5)
        self.assertEqual(metrics["average_trade"], 3.0)
        self.assertEqual(metrics["expectancy"], 3.0)
        self.assertEqual(metrics["spread_cost"], 1.5)
        self.assertEqual(metrics["slippage"], 0.75)
        self.assertFalse(metrics["profitability_claim"])

    def test_live_paper_label_rejected_for_historical_campaign(self) -> None:
        store = _build_historical_store()
        with self.assertRaises(GrowConfigError) as ctx:
            HistoricalValidationCampaign(
                store,
                risk_secret=TEST_RISK_SECRET,
                config=_config(),
                qualification=_qualification(store),
                evaluation_label="LIVE-PAPER",
            )
        self.assertTrue(
            "LIVE_PAPER_NOT_HISTORICAL_VALIDATION" in str(ctx.exception)
            or "EVALUATION_LABEL_MIXED" in str(ctx.exception)
        )

    def test_no_broker_order_api_in_historical_validation(self) -> None:
        import grow.validation.historical as hist
        import grow.validation.labels as labels

        for module in (hist, labels):
            source = inspect.getsource(module)
            tree = ast.parse(source)
            calls = [
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
            ]
            for forbidden in ("place_order", "place_live_order", "LiveBroker"):
                self.assertNotIn(forbidden, calls)
            self.assertNotIn("place_order", source)
            self.assertNotIn("LiveBroker", source)


if __name__ == "__main__":
    unittest.main()
