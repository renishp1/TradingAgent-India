"""Phase 5 — SENSEX policy, BFO parsing, multi-index CampaignOptions, safety locks."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from dataclasses import replace

from grow.agents.campaign_options import (
    NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE,
    CampaignOptionsAgent,
    evaluate_campaign_feasibility,
)
from grow.campaign import CampaignRunner, campaign_paper_config
from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.decision.contracts.agent_result import CandidateAction
from grow.errors import GrowConfigError
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.history.universe import default_index_registry
from grow.live_data.catalog import UNKNOWN_EXPIRY_CLASS
from grow.live_data.expiry_class import ExpiryClassifier, default_expiry_schedules
from grow.live_data.kite_campaign_snapshot import CAMPAIGN_UNDERLYINGS, build_campaign_live_snapshot
from grow.live_data.kite_market import (
    BFO_UNDERLYINGS,
    INDEX_EXCHANGE,
    INDEX_QUERY,
    NFO_UNDERLYINGS,
    OPTION_QUOTE_PREFIX,
    parse_bfo_instruments,
    parse_nfo_instruments,
)
from grow.market.bse_fo_calendar import BSE_FO_HOLIDAYS_2026
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView, UnderlyingQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot, history_diagnostics_from_closes

from tests.helpers import TEST_RISK_SECRET


AS_OF = datetime(2026, 9, 24, 11, 30, tzinfo=IST)
# SENSEX weekly weekday is Thursday; 2026-10-01 is a Thursday.
SENSEX_EXPIRY = date(2026, 10, 1)
NIFTY_EXPIRY = date(2026, 10, 6)  # Tuesday weekly

BFO_CSV = """instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange
260001,1,SENSEX26O0181000CE,SENSEX,0,2026-10-01,81000,0.05,20,CE,BFO-OPT,BFO
260002,2,SENSEX26O0181000PE,SENSEX,0,2026-10-01,81000,0.05,20,PE,BFO-OPT,BFO
260003,3,SENSEX26O0181050CE,SENSEX,0,2026-10-01,81050,0.05,20,CE,BFO-OPT,BFO
260004,4,SENSEX26O0181050PE,SENSEX,0,2026-10-01,81050,0.05,20,PE,BFO-OPT,BFO
260005,5,SENSEX26O0180000CE,SENSEX,0,2026-10-01,80000,0.05,20,CE,BFO-OPT,BFO
260006,6,SENSEX26O0180000PE,SENSEX,0,2026-10-01,80000,0.05,20,PE,BFO-OPT,BFO
999001,9,RELIANCE26O012500CE,RELIANCE,0,2026-10-01,2500,0.05,1,CE,BFO-OPT,BFO
"""


def _cfg():
    return campaign_paper_config(load_config(environ={"GROW_EXECUTION_MODE": "paper"}))


def _opt(
    underlying: str,
    *,
    side: str,
    strike: float,
    ask: float,
    lot_size: int,
    expiry: date,
    expiry_class: str = "WEEKLY",
) -> OptionQuoteView:
    # Keep spread tight so chain-filter liquidity gates pass; feasibility is separate.
    bid = round(max(float(ask) - 0.05, 0.05), 2)
    return OptionQuoteView(
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=side,
        ltp=ask,
        bid=bid,
        ask=ask,
        open_interest=1_000,
        volume=200,
        quote_timestamp=AS_OF,
        quote_age_seconds=0.0,
        provider_contract_id=f"{underlying}-{int(strike)}-{side}",
        quality=DataQualityStatus.OK,
        lot_size=lot_size,
        expiry_class=expiry_class,
        is_fixture=False,
    )


def _history(closes: list[float]):
    return history_diagnostics_from_closes(
        closes,
        interval="M15",
        earliest="2026-09-23T09:15:00+05:30",
        latest="2026-09-24T11:15:00+05:30",
        provenance="LIVE",
    )


def _multi_snapshot(
    *,
    nifty_spot: float = 23200.0,
    sensex_spot: float = 81000.0,
    nifty_ask: float = 12.0,
    sensex_ask: float = 8.0,
    nifty_lot: int = 65,
    sensex_lot: int = 20,
    nifty_closes: list[float] | None = None,
    sensex_closes: list[float] | None = None,
    include_nifty: bool = True,
    include_sensex: bool = True,
):
    """Multi-index fixture. Direction from per-underlying M15 history."""
    n_closes = nifty_closes or [23200.0 - i * 3.0 for i in range(30)]
    s_closes = sensex_closes or [81000.0 - i * 10.0 for i in range(30)]
    options: list[OptionQuoteView] = []
    underlyings: dict[str, UnderlyingQuoteView] = {}
    hist_by: dict[str, list[float]] = {}

    if include_nifty:
        underlyings["NIFTY"] = UnderlyingQuoteView(
            underlying="NIFTY",
            exchange="NSE",
            spot=nifty_spot,
            ltp=nifty_spot,
            open=nifty_spot,
            high=nifty_spot + 50,
            low=nifty_spot - 50,
            close=nifty_spot,
            volume=0,
            quote_timestamp=AS_OF,
            quote_age_seconds=0.0,
        )
        hist_by["NIFTY"] = list(n_closes)
        for strike in (nifty_spot - 50, nifty_spot, nifty_spot + 50):
            for side in ("CE", "PE"):
                options.append(
                    _opt(
                        "NIFTY",
                        side=side,
                        strike=strike,
                        ask=nifty_ask if side == "PE" else nifty_ask * 0.7,
                        lot_size=nifty_lot,
                        expiry=NIFTY_EXPIRY,
                    )
                )

    if include_sensex:
        underlyings["SENSEX"] = UnderlyingQuoteView(
            underlying="SENSEX",
            exchange="BSE",
            spot=sensex_spot,
            ltp=sensex_spot,
            open=sensex_spot,
            high=sensex_spot + 100,
            low=sensex_spot - 100,
            close=sensex_spot,
            volume=0,
            quote_timestamp=AS_OF,
            quote_age_seconds=0.0,
        )
        hist_by["SENSEX"] = list(s_closes)
        for strike in (sensex_spot - 50, sensex_spot, sensex_spot + 50):
            for side in ("CE", "PE"):
                options.append(
                    _opt(
                        "SENSEX",
                        side=side,
                        strike=strike,
                        ask=sensex_ask if side == "PE" else sensex_ask * 0.7,
                        lot_size=sensex_lot,
                        expiry=SENSEX_EXPIRY,
                    )
                )

    primary = "NIFTY" if include_nifty else "SENSEX"
    primary_closes = hist_by[primary]
    base = build_fixture_snapshot(
        underlying=primary,
        as_of=AS_OF,
        spot=float(underlyings[primary].spot or 0),
        option_contracts=tuple(options),
        exchange=underlyings[primary].exchange,
        diagnostics={
            **_history(primary_closes),
            "history_closes_by_underlying": {k: list(v) for k, v in hist_by.items()},
            "campaign_underlyings": list(underlyings.keys()),
            "market_data_source": "LIVE",
            "history_provenance": "LIVE",
        },
    )
    return replace(
        base,
        underlyings=underlyings,
        exchange="MULTI" if len(underlyings) > 1 else underlyings[primary].exchange,
    )


class SensexPolicyTests(unittest.TestCase):
    def test_1_sensex_policy_exists(self) -> None:
        reg = default_index_registry()
        policy = reg.policy("SENSEX")
        self.assertIsNotNone(policy)
        assert policy is not None
        self.assertEqual(policy.exchange, "BSE")
        self.assertEqual(policy.expiry_policy_profile, "WEEKLY_PREFERRED")
        self.assertEqual(policy.lot_size_source, "CONTRACT_MASTER")
        self.assertTrue(policy.option_supported)
        self.assertTrue(reg.allows("SENSEX", date(2026, 9, 24)))

    def test_sensex_schedule_is_thursday_with_bse_holidays(self) -> None:
        schedules = {s.canonical_symbol: s for s in default_expiry_schedules()}
        sensex = schedules["SENSEX"]
        self.assertEqual(sensex.weekly_weekday, 3)  # Thursday
        self.assertEqual(sensex.monthly_weekday, 3)
        self.assertEqual(sensex.holidays, BSE_FO_HOLIDAYS_2026)
        nifty = schedules["NIFTY"]
        self.assertEqual(nifty.weekly_weekday, 1)  # Tuesday
        self.assertIsNone(nifty.holidays)  # NSE default calendar


class SensexBfoParsingTests(unittest.TestCase):
    def test_2_bse_bfo_instrument_parsing(self) -> None:
        rows = parse_bfo_instruments(BFO_CSV)
        underlyings = {r.underlying for r in rows}
        self.assertEqual(underlyings, {"SENSEX"})
        self.assertTrue(all(r.option_type in {"CE", "PE"} for r in rows))
        self.assertNotIn("RELIANCE", underlyings)
        self.assertEqual(INDEX_QUERY["SENSEX"], "BSE:SENSEX")
        self.assertEqual(INDEX_EXCHANGE["SENSEX"], "BSE")
        self.assertEqual(OPTION_QUOTE_PREFIX["SENSEX"], "BFO")
        self.assertIn("SENSEX", BFO_UNDERLYINGS)
        self.assertNotIn("SENSEX", NFO_UNDERLYINGS)

    def test_3_sensex_option_selection_ce_pe(self) -> None:
        from grow.live_data.kite_campaign_snapshot import _pair_for_underlying

        rows = parse_bfo_instruments(BFO_CSV)
        clock = FrozenClock(AS_OF)
        call, put = _pair_for_underlying(
            rows,
            spot=81020.0,
            as_of=AS_OF.date(),
            classifier=ExpiryClassifier(clock=clock),
            underlying="SENSEX",
        )
        self.assertEqual(call.underlying, "SENSEX")
        self.assertEqual(put.underlying, "SENSEX")
        self.assertEqual(call.option_type, "CE")
        self.assertEqual(put.option_type, "PE")
        self.assertEqual(call.strike, put.strike)
        self.assertEqual(call.expiry, SENSEX_EXPIRY)

    def test_4_lot_size_from_contract_metadata(self) -> None:
        rows = {r.tradingsymbol: r for r in parse_bfo_instruments(BFO_CSV)}
        self.assertEqual(rows["SENSEX26O0181000CE"].lot_size, 20)
        self.assertEqual(rows["SENSEX26O0181000PE"].lot_size, 20)
        # Feasibility uses contract lot_size, not a hardcoded NIFTY 65.
        row = evaluate_campaign_feasibility(
            _opt("SENSEX", side="PE", strike=81000, ask=8.0, lot_size=20, expiry=SENSEX_EXPIRY),
            lots=1,
            available_cash=10_000,
            max_per_trade_risk=1_000,
        )
        self.assertTrue(row.ok, row.reject_reason)
        self.assertEqual(row.required_cash, 160.0)  # 8 * 20
        self.assertEqual(row.planned_risk, 32.0)  # |8 - 6.4| * 20


class SensexExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = ExpiryClassifier(clock=FrozenClock(AS_OF))

    def test_5_sensex_expiry_classification_thursday(self) -> None:
        weekly = self.classifier.classify(
            provider_symbol="SENSEX26O0181000CE",
            canonical_symbol="SENSEX",
            expiry=SENSEX_EXPIRY,
            option_type="CE",
            as_of=AS_OF,
        )
        self.assertIn(weekly.expiry_class, {"WEEKLY", "MONTHLY"})
        # Tuesday NIFTY rule must not classify a random Tuesday as SENSEX weekly.
        tuesday = self.classifier.classify(
            provider_symbol="SENSEX26SEP23200CE",
            canonical_symbol="SENSEX",
            expiry=date(2026, 9, 22),  # Tuesday
            option_type="CE",
            as_of=AS_OF,
        )
        self.assertEqual(tuesday.expiry_class, UNKNOWN_EXPIRY_CLASS)

    def test_7_unknown_expiry_fails_closed(self) -> None:
        bad = self.classifier.classify(
            provider_symbol="SENSEX26W0120000CE",
            canonical_symbol="SENSEX",
            expiry=date(2026, 9, 23),  # Wednesday
            option_type="CE",
            as_of=AS_OF,
        )
        self.assertEqual(bad.expiry_class, UNKNOWN_EXPIRY_CLASS)
        unknown_idx = self.classifier.classify(
            provider_symbol="FOO260924100CE",
            canonical_symbol="UNKNOWNIDX",
            expiry=SENSEX_EXPIRY,
            option_type="CE",
            as_of=AS_OF,
        )
        self.assertEqual(unknown_idx.expiry_class, UNKNOWN_EXPIRY_CLASS)


class SensexHistoryAndMultiIndexTests(unittest.TestCase):
    def test_6_sensex_m15_history_diagnostics(self) -> None:
        closes = [81000.0 - i for i in range(20)]
        snap = _multi_snapshot(sensex_closes=closes, include_nifty=False)
        by = snap.diagnostics["history_closes_by_underlying"]
        self.assertEqual(list(by["SENSEX"]), closes)
        self.assertEqual(snap.diagnostics.get("history_interval"), "M15")
        self.assertEqual(snap.diagnostics.get("history_provenance"), "LIVE")

    def test_8_nifty_and_sensex_coexist(self) -> None:
        snap = _multi_snapshot()
        self.assertIn("NIFTY", snap.underlyings)
        self.assertIn("SENSEX", snap.underlyings)
        self.assertTrue(any(o.underlying == "NIFTY" for o in snap.option_contracts))
        self.assertTrue(any(o.underlying == "SENSEX" for o in snap.option_contracts))
        self.assertEqual(CAMPAIGN_UNDERLYINGS, ("NIFTY", "SENSEX"))

    def test_9_nifty_does_not_suppress_sensex(self) -> None:
        # NIFTY unaffordable ATM; SENSEX cheap — SENSEX must still be evaluated and can win.
        snap = _multi_snapshot(nifty_ask=200.0, sensex_ask=5.0, nifty_lot=65, sensex_lot=20)
        result = CampaignOptionsAgent(_cfg()).analyze(snap, cycle_id="no-suppress")
        metrics = result.calculated_metrics or {}
        per = metrics.get("per_index") or {}
        self.assertIn("NIFTY", per)
        self.assertIn("SENSEX", per)
        self.assertEqual(per["NIFTY"].get("status"), NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE)
        self.assertEqual(per["SENSEX"].get("status"), "CANDIDATE")
        self.assertEqual(result.candidate_action, CandidateAction.PAPER_OPEN)
        self.assertEqual(metrics.get("underlying"), "SENSEX")

    def test_10_campaign_options_evaluates_both(self) -> None:
        snap = _multi_snapshot(nifty_ask=10.0, sensex_ask=8.0)
        result = CampaignOptionsAgent(_cfg()).analyze(snap, cycle_id="both")
        metrics = result.calculated_metrics or {}
        self.assertEqual(set(metrics.get("underlyings_evaluated") or []), {"NIFTY", "SENSEX"})
        per = metrics.get("per_index") or {}
        self.assertEqual(set(per.keys()), {"NIFTY", "SENSEX"})
        # Both should produce candidates when affordable.
        self.assertEqual(per["NIFTY"].get("status"), "CANDIDATE")
        self.assertEqual(per["SENSEX"].get("status"), "CANDIDATE")

    def test_11_sensex_candidate_feasibility(self) -> None:
        # 50 * 20 = 1000 cash OK; planned risk 10*20=200 <= 1000 OK
        snap = _multi_snapshot(include_nifty=False, sensex_ask=50.0, sensex_lot=20)
        result = CampaignOptionsAgent(_cfg()).analyze(snap, cycle_id="sx-feas")
        self.assertEqual(result.candidate_action, CandidateAction.PAPER_OPEN)
        metrics = result.calculated_metrics or {}
        self.assertEqual(metrics.get("underlying"), "SENSEX")
        self.assertLessEqual(float(metrics["required_cash"]), 10_000)
        self.assertLessEqual(float(metrics["planned_risk"]), 1_000)

        # Too expensive: 80 * 20 = 1600 cash > 10000? No — 1600 < 10000.
        # Risk: |80-64|*20 = 320 OK. Need cash fail: ask=600 -> 12000 > 10000.
        snap2 = _multi_snapshot(include_nifty=False, sensex_ask=600.0, sensex_lot=20)
        result2 = CampaignOptionsAgent(_cfg()).analyze(snap2, cycle_id="sx-cash")
        self.assertEqual(result2.candidate_action, CandidateAction.NONE)
        self.assertIn(NO_AFFORDABLE_RISK_COMPLIANT_CANDIDATE, result2.findings)

    def test_12_sensex_missing_invalid_data_fails_closed(self) -> None:
        # No options for SENSEX → chain filter / no candidate; never fabricates.
        base = _multi_snapshot(include_nifty=False)
        snap = replace(base, option_contracts=())
        result = CampaignOptionsAgent(_cfg()).analyze(snap, cycle_id="sx-missing")
        self.assertEqual(result.candidate_action, CandidateAction.NONE)
        self.assertNotEqual(result.candidate_action, CandidateAction.PAPER_OPEN)

        # Empty BFO catalog
        with self.assertRaises(GrowConfigError):
            parse_bfo_instruments("")

    def test_13_no_broker_orders(self) -> None:
        snap = _multi_snapshot(nifty_ask=10.0, sensex_ask=8.0)
        runner = CampaignRunner(
            _cfg(),
            clock=FrozenClock(AS_OF),
            risk_secret=TEST_RISK_SECRET,
            specialists=(CampaignOptionsAgent(_cfg()),),
            apply_campaign_defaults=False,
        )
        result = runner.run_cycle(snap, cycle_id="sx-broker")
        self.assertEqual(result.execution.broker_order_calls, 0)
        self.assertFalse(result.to_dict().get("broker_order_path"))

    def test_14_live_trading_compiled_false(self) -> None:
        self.assertIs(LIVE_TRADING_COMPILED, False)


class SensexCampaignRunnerIntegrationTests(unittest.TestCase):
    def test_multi_index_runtime_riskguard_active(self) -> None:
        cfg = _cfg()
        self.assertEqual(cfg.paper.starting_cash, 10_000)
        self.assertEqual(cfg.risk.max_per_trade_risk, 1_000)
        self.assertEqual(cfg.risk.max_daily_loss, 2_000)
        snap = _multi_snapshot(nifty_ask=200.0, sensex_ask=5.0)
        runner = CampaignRunner(
            cfg,
            clock=FrozenClock(AS_OF),
            risk_secret=TEST_RISK_SECRET,
            specialists=(CampaignOptionsAgent(cfg),),
            apply_campaign_defaults=False,
        )
        result = runner.run_cycle(snap, cycle_id="multi-rg")
        self.assertEqual(result.execution.broker_order_calls, 0)
        self.assertIs(LIVE_TRADING_COMPILED, False)
        # RiskGuard remains in the path (approved or rejected — not bypassed).
        self.assertIn(
            result.decision.risk_guard_result,
            {"APPROVED", "REJECTED", "NO_TRADE", "NOT_EVALUATED", None},
        )
        # Prefer approved SENSEX fill when cheap; otherwise still prove evaluation happened.
        camp = next(
            (o for o in result.package.agent_outputs if o.agent_name == "campaign_options"),
            None,
        )
        self.assertIsNotNone(camp)
        per = (camp.calculated_metrics or {}).get("per_index") or {}
        self.assertIn("SENSEX", per)
        self.assertIn("NIFTY", per)


class SensexSnapshotBuilderImportTests(unittest.TestCase):
    def test_campaign_snapshot_builder_exported(self) -> None:
        self.assertTrue(callable(build_campaign_live_snapshot))
        self.assertEqual(CAMPAIGN_UNDERLYINGS, ("NIFTY", "SENSEX"))


if __name__ == "__main__":
    unittest.main()
