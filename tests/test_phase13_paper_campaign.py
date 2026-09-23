"""Phase 13 — multi-session paper campaign + per-session EOD reports."""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from grow.campaign import (
    EOD_SCHEMA,
    PAPER_CAMPAIGN_VERSION,
    PaperCampaign,
    PaperCampaignReport,
    SessionEODReport,
    SessionFeed,
    assert_campaign_snapshot_label,
    build_session_eod_report,
    campaign_paper_config,
)
from grow.campaign.session import PaperSessionRunner
from grow.clock import IST, FrozenClock
from grow.config import load_config
from grow.decision.contracts.agent_result import AgentResult, AgentStatus, CandidateAction
from grow.errors import GrowConfigError, GrowSafetyError
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.provenance import MarketDataSource
from grow.market_data.snapshots.builder import build_fixture_snapshot
from grow.validation.labels import EvaluationLabel

from tests.helpers import TEST_RISK_SECRET


AS_OF = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
EXPIRY = date(2026, 9, 24)


def _quote(as_of=AS_OF, **overrides):
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
        quote_timestamp=as_of,
        quote_age_seconds=0.0,
        provider_contract_id="RELIANCE-2500-CE",
        quality=DataQualityStatus.OK,
        lot_size=1,
        expiry_class="WEEKLY",
    )
    payload.update(overrides)
    return OptionQuoteView(**payload)


def _fixture_snapshot(as_of=AS_OF, *, quotes=None, quality=DataQualityStatus.OK):
    if quotes is None:
        quotes = (_quote(as_of),)
    return build_fixture_snapshot(
        underlying="RELIANCE",
        as_of=as_of,
        spot=2500.0,
        option_contracts=tuple(quotes),
        quality=quality,
        notes=("phase13",) if quality is not DataQualityStatus.OK else (),
    )


def _live_snapshot(as_of=AS_OF, *, quotes=None):
    """Non-fixture snapshot for LIVE-PAPER campaign tests."""
    snap = _fixture_snapshot(as_of, quotes=quotes)
    live_options = tuple(replace(row, is_fixture=False) for row in snap.option_contracts)
    return replace(snap, option_contracts=live_options, provider="kite.paper.live.test")


def _feed(day: date, *, live: bool = False, quality=DataQualityStatus.OK) -> SessionFeed:
    open_at = datetime(day.year, day.month, day.day, 11, 0, tzinfo=IST)
    later = open_at + timedelta(minutes=5)
    builder = _live_snapshot if live else _fixture_snapshot
    first = builder(open_at, quotes=(_quote(open_at),))
    if quality is DataQualityStatus.OK:
        second = builder(later, quotes=(_quote(later, ltp=110.0, bid=109.0, ask=111.0),))
    else:
        second = builder(later, quality=quality)
    return SessionFeed(
        session_date=day,
        snapshots=(first, second),
        reconnects=({"at": later.isoformat(), "reason": "WS_RECONNECT"},) if live else (),
        stale_events=({"snapshot_id": "pre", "data_quality": "STALE"},)
        if quality is DataQualityStatus.STALE
        else (),
    )


class _OpenSpecialist:
    agent_name = "strategy_research"
    agent_version = "strategy_research.v2"

    def analyze(self, snapshot, *, cycle_id: str = ""):
        return AgentResult(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            decision_timestamp=snapshot.decision_timestamp,
            status=AgentStatus.PASS,
            observations=("phase13",),
            calculated_metrics={
                "strategy": "trend",
                "direction": "BULLISH",
                "underlying": "RELIANCE",
                "limit_price": 100.0,
                "stop_loss": 80.0,
                "target": 140.0,
                "lots": 1,
                "quantity": 1,
                "option_type": "CE",
                "strike": 2500.0,
                "expiry": EXPIRY.isoformat(),
                "lot_size": 1,
            },
            interpretation=(),
            findings=("UNANIMOUS_OPEN",),
            data_quality_concerns=(),
            assumptions=("buyer-only",),
            evidence=(f"snapshot_id={snapshot.snapshot_id}",),
            metrics_used=("limit_price", "stop_loss", "quantity"),
            candidate_action=CandidateAction.PAPER_OPEN,
            candidate_instrument="RELIANCE-2500-CE",
            entry_reason="phase13",
            invalidation_reason=None,
            risk_flags=(),
            missing_data=(),
            confidence=0.55,
            cycle_id=cycle_id,
        )

    def analyze_input(self, agent_input):
        return self.analyze(agent_input.snapshot, cycle_id=agent_input.cycle_id)


class _AbstainSpecialist(_OpenSpecialist):
    def analyze(self, snapshot, *, cycle_id: str = ""):
        base = super().analyze(snapshot, cycle_id=cycle_id)
        return replace(
            base,
            findings=("NO_SETUP",),
            candidate_action=CandidateAction.ABSTAIN,
            candidate_instrument=None,
            confidence=0.1,
        )


def _config():
    return campaign_paper_config(load_config())


class Phase13PaperCampaignTests(unittest.TestCase):
    def test_multi_session_fixture_campaign_emits_eod_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaign = PaperCampaign(
                _config(),
                risk_secret=TEST_RISK_SECRET,
                evaluation_label="FIXTURE",
                artifact_dir=tmp,
                specialists=(_OpenSpecialist(),),
                apply_campaign_defaults=False,
            )
            feeds = (
                _feed(date(2026, 9, 21)),
                _feed(date(2026, 9, 22)),
                _feed(date(2026, 9, 23)),
            )
            report = campaign.run(feeds)
            self.assertEqual(report.session_count, 3)
            self.assertEqual(report.evaluation_label, "FIXTURE")
            self.assertFalse(report.profitability_claim)
            self.assertEqual(report.broker_order_calls, 0)
            self.assertEqual(len(report.eod_paths), 3)
            for path in report.eod_paths:
                body = json.loads(Path(path).read_text(encoding="utf-8"))
                self.assertEqual(body["schema"], EOD_SCHEMA)
                self.assertIn("answers", body)
                for key in (
                    "why_traded",
                    "why_not_traded",
                    "why_rejected",
                    "data_available",
                    "agent_statements",
                    "risk_guard_statements",
                    "prices_used",
                    "simulated_pnl",
                ):
                    self.assertIn(key, body["answers"])
                for key in (
                    "data_failures",
                    "reconnects",
                    "stale_events",
                    "decision_latency",
                    "agent_latency",
                    "execution_simulation",
                    "slippage",
                    "spread",
                    "risk_guard_blocks",
                ):
                    self.assertIn(key, body["telemetry"])
                self.assertFalse(body["live_trading"])
                self.assertEqual(body["broker_order_calls"], 0)

    def test_eod_answers_why_traded_and_agent_risk_evidence(self) -> None:
        campaign = PaperCampaign(
            _config(),
            risk_secret=TEST_RISK_SECRET,
            evaluation_label="FIXTURE",
            specialists=(_OpenSpecialist(),),
            apply_campaign_defaults=False,
        )
        eod = campaign.run_session(_feed(date(2026, 9, 22)))
        self.assertIsInstance(eod, SessionEODReport)
        self.assertTrue(eod.answers["why_traded"] or eod.answers["why_not_traded"] or eod.answers["why_rejected"])
        self.assertTrue(eod.answers["agent_statements"])
        self.assertTrue(eod.answers["risk_guard_statements"])
        self.assertIn("net_pnl", eod.answers["simulated_pnl"])
        self.assertFalse(eod.profitability_claim)

    def test_live_paper_rejects_fixture_snapshots(self) -> None:
        campaign = PaperCampaign(
            _config(),
            risk_secret=TEST_RISK_SECRET,
            evaluation_label="LIVE-PAPER",
            specialists=(_AbstainSpecialist(),),
            apply_campaign_defaults=False,
        )
        with self.assertRaises(GrowConfigError) as ctx:
            campaign.run_session(_feed(date(2026, 9, 22), live=False))
        self.assertIn("LIVE_PAPER_FIXTURE_SNAPSHOT_FORBIDDEN", str(ctx.exception))

    def test_live_paper_accepts_non_fixture_snapshots(self) -> None:
        campaign = PaperCampaign(
            _config(),
            risk_secret=TEST_RISK_SECRET,
            evaluation_label=EvaluationLabel.LIVE_PAPER,
            specialists=(_AbstainSpecialist(),),
            apply_campaign_defaults=False,
        )
        feed = _feed(date(2026, 9, 22), live=True)
        self.assertEqual(feed.snapshots[0].market_data_source, MarketDataSource.LIVE)
        eod = campaign.run_session(feed)
        self.assertEqual(eod.evaluation_label, "LIVE-PAPER")
        self.assertEqual(eod.telemetry["reconnects"][0]["reason"], "WS_RECONNECT")
        self.assertFalse(eod.to_dict()["live_trading"])

    def test_mixed_snapshot_provenance_rejected(self) -> None:
        day = date(2026, 9, 22)
        open_at = datetime(day.year, day.month, day.day, 11, 0, tzinfo=IST)
        fixture = _fixture_snapshot(open_at)
        live = _live_snapshot(open_at + timedelta(minutes=1))
        with self.assertRaises(GrowConfigError) as ctx:
            assert_campaign_snapshot_label("FIXTURE", (fixture, live))
        self.assertIn("PAPER_CAMPAIGN_SNAPSHOT_PROVENANCE_MIXED", str(ctx.exception))

    def test_live_paper_rejects_quote_level_mixed_provenance(self) -> None:
        """LIVE-intended snapshot with one fixture option quote → MIXED → reject."""
        day = date(2026, 9, 22)
        as_of = datetime(day.year, day.month, day.day, 11, 0, tzinfo=IST)
        live_ce = replace(_quote(as_of), is_fixture=False)
        fixture_pe = replace(
            _quote(as_of, option_type="PE", provider_contract_id="RELIANCE-2500-PE", strike=2500.0),
            is_fixture=True,
        )
        # Top-level builder starts fixture; replace options so classification is MIXED.
        base = build_fixture_snapshot(
            underlying="RELIANCE",
            as_of=as_of,
            spot=2500.0,
            option_contracts=(live_ce, fixture_pe),
            provider="kite.paper.live.test",
        )
        mixed = replace(base, option_contracts=(live_ce, fixture_pe), provider="kite.paper.live.test")
        self.assertEqual(mixed.market_data_source, MarketDataSource.MIXED)
        self.assertFalse(mixed.is_fixture)

        campaign = PaperCampaign(
            _config(),
            risk_secret=TEST_RISK_SECRET,
            evaluation_label="LIVE-PAPER",
            specialists=(_AbstainSpecialist(),),
            apply_campaign_defaults=False,
        )
        feed = SessionFeed(session_date=day, snapshots=(mixed,))
        with self.assertRaises(GrowConfigError) as ctx:
            campaign.run_session(feed)
        self.assertIn("MIXED_MARKET_DATA_SOURCE", str(ctx.exception))
        self.assertEqual(campaign.eod_reports, ())
        self.assertEqual(campaign.status, "CREATED")
        # Fail-closed with no completed session and no broker activity claim.
        with self.assertRaises(GrowConfigError):
            campaign.report()

    def test_worst_session_drawdown_is_min_of_session_drawdowns(self) -> None:
        campaign = PaperCampaign(
            _config(),
            risk_secret=TEST_RISK_SECRET,
            evaluation_label="FIXTURE",
            specialists=(_AbstainSpecialist(),),
            apply_campaign_defaults=False,
        )
        base = campaign.run_session(_feed(date(2026, 9, 21)))
        # Inject known non-positive session drawdowns; engines are per-session so
        # campaign drawdown is worst single-session value, not continuous capital.
        campaign._eods = [
            replace(base, summary={**dict(base.summary), "max_drawdown": -500.0}, session_date=date(2026, 9, 21)),
            replace(base, summary={**dict(base.summary), "max_drawdown": -1500.0}, session_date=date(2026, 9, 22)),
            replace(base, summary={**dict(base.summary), "max_drawdown": -250.0}, session_date=date(2026, 9, 23)),
        ]
        report = campaign.report()
        self.assertEqual(report.worst_session_drawdown, -1500.0)
        payload = report.to_dict()
        self.assertEqual(payload["worst_session_drawdown"], -1500.0)
        self.assertNotIn("combined_max_drawdown", payload)

    def test_campaign_run_is_one_shot(self) -> None:
        feeds = (
            _feed(date(2026, 9, 21)),
            _feed(date(2026, 9, 22)),
        )
        campaign = PaperCampaign(
            _config(),
            risk_secret=TEST_RISK_SECRET,
            evaluation_label="FIXTURE",
            specialists=(_AbstainSpecialist(),),
            apply_campaign_defaults=False,
        )
        first = campaign.run(feeds)
        self.assertEqual(first.session_count, 2)
        self.assertEqual(campaign.status, "ENDED")
        self.assertEqual(first.broker_order_calls, 0)
        self.assertEqual(first.to_dict()["broker_order_calls"], 0)
        with self.assertRaises(GrowSafetyError) as ctx:
            campaign.run(feeds)
        self.assertIn("construct a fresh PaperCampaign", str(ctx.exception))
        self.assertEqual(len(campaign.eod_reports), 2)
        with self.assertRaises(GrowSafetyError):
            campaign.run_session(_feed(date(2026, 9, 23)))
        self.assertEqual(len(campaign.eod_reports), 2)
        # Rerun rejection leaves broker_order_calls at zero on the completed report.
        self.assertEqual(campaign.report().broker_order_calls, 0)

    def test_historical_label_forbidden_for_paper_campaign(self) -> None:
        with self.assertRaises(GrowConfigError) as ctx:
            PaperCampaign(
                _config(),
                risk_secret=TEST_RISK_SECRET,
                evaluation_label="HISTORICAL",
                specialists=(_AbstainSpecialist(),),
                apply_campaign_defaults=False,
            )
        self.assertIn("PAPER_CAMPAIGN_LABEL_FORBIDDEN", str(ctx.exception))

    def test_stale_snapshot_skips_new_decision_but_recorded_in_eod(self) -> None:
        campaign = PaperCampaign(
            _config(),
            risk_secret=TEST_RISK_SECRET,
            evaluation_label="FIXTURE",
            specialists=(_OpenSpecialist(),),
            apply_campaign_defaults=False,
        )
        day = date(2026, 9, 22)
        open_at = datetime(day.year, day.month, day.day, 11, 0, tzinfo=IST)
        feed = SessionFeed(
            session_date=day,
            snapshots=(
                _fixture_snapshot(open_at),
                _fixture_snapshot(open_at + timedelta(minutes=5), quality=DataQualityStatus.STALE),
            ),
            stale_events=({"note": "provider_stale"},),
        )
        eod = campaign.run_session(feed)
        self.assertTrue(eod.telemetry["stale_events"])
        # First snapshot may trade; stale second must not add a decision cycle.
        self.assertLessEqual(len(eod.summary["decisions"]), 1)

    def test_each_session_uses_fresh_paper_session_runner(self) -> None:
        campaign = PaperCampaign(
            _config(),
            risk_secret=TEST_RISK_SECRET,
            evaluation_label="FIXTURE",
            specialists=(_AbstainSpecialist(),),
            apply_campaign_defaults=False,
        )
        first = campaign.run_session(_feed(date(2026, 9, 21)))
        second = campaign.run_session(_feed(date(2026, 9, 22)))
        self.assertNotEqual(first.session_id, second.session_id)
        report = campaign.report()
        self.assertIsInstance(report, PaperCampaignReport)
        self.assertEqual(report.session_count, 2)

    def test_campaign_forbids_live_trading_config(self) -> None:
        config = _config()
        bad = replace(config, live_data=replace(config.live_data, live_trading=True, paper_mode=False))
        with self.assertRaises((GrowSafetyError, Exception)):
            PaperCampaign(
                bad,
                risk_secret=TEST_RISK_SECRET,
                evaluation_label="FIXTURE",
                specialists=(_AbstainSpecialist(),),
                apply_campaign_defaults=False,
            )

    def test_no_broker_order_api_in_phase13_modules(self) -> None:
        import grow.campaign.eod as eod
        import grow.campaign.paper_campaign as paper_campaign

        for module in (eod, paper_campaign):
            source = inspect.getsource(module)
            self.assertNotIn("place_order", source)
            self.assertNotIn("LiveBroker", source)
            self.assertNotIn("place_live_order", source)
            tree = ast.parse(source)
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            self.assertNotIn("place_order", names)

    def test_reuses_existing_session_runner_not_third_executor(self) -> None:
        source = inspect.getsource(PaperCampaign.run_session)
        self.assertIn("PaperSessionRunner", source)
        self.assertIn("CampaignRunner", source)
        self.assertNotIn("LivePaperLoop", source)


if __name__ == "__main__":
    unittest.main()
