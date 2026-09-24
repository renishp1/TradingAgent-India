"""RiskGuard universe: cash market book ∪ strategies.universe (NIFTY/BANKNIFTY)."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from uuid import uuid4

from grow.agents.campaign_options import CampaignOptionsAgent
from grow.campaign import CampaignRunner, campaign_paper_config
from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.decision.contracts.agent_result import CandidateAction
from grow.decision.integration.contract import DecisionAction, IntegratedDecisionStatus
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot, history_diagnostics_from_closes
from grow.risk.guard import RiskGuard, risk_allowed_tickers
from grow.types import Intent, MarketBrief, Regime, SessionState, Side, Symbol, TradeProposal, Venue

from tests.helpers import TEST_RISK_SECRET, make_guard


AS_OF = datetime(2026, 9, 24, 11, 56, tzinfo=IST)
EXPIRY = date(2026, 10, 6)


def _brief(symbol: Symbol, price: float = 100.0) -> MarketBrief:
    return MarketBrief(
        symbol=symbol,
        as_of=AS_OF,
        session=SessionState.OPEN,
        last_price=price,
        currency="INR",
        regime=Regime.UNKNOWN,
        notes=(),
        source="test",
    )


def _proposal(ticker: str, *, price: float = 100.0, qty: int = 1) -> TradeProposal:
    return TradeProposal(
        proposal_id=f"t-{uuid4().hex[:8]}",
        symbol=Symbol(ticker),
        side=Side.BUY,
        intent=Intent.OPEN,
        quantity=qty,
        limit_price=price,
        stop_loss=round(price * 0.8, 2),
        take_profit=round(price * 1.2, 2),
        thesis="universe-test",
        confidence=0.5,
        venue=Venue.PAPER,
        created_at=AS_OF,
        notional=round(qty * price, 2),
    )


def _nifty_option_snap(*, premium: float = 20.0, lot_size: int = 65):
    """Affordable PE so universe is the gate under test (not cash/risk caps)."""
    closes = [23200.0 - i * 3.0 for i in range(30)]
    spot = closes[-1]
    quotes = tuple(
        OptionQuoteView(
            underlying="NIFTY",
            expiry=EXPIRY,
            strike=strike,
            option_type=side,
            ltp=premium if side == "PE" else premium * 0.6,
            bid=(premium if side == "PE" else premium * 0.6) - 0.5,
            ask=(premium if side == "PE" else premium * 0.6) + 0.5,
            open_interest=1_000,
            volume=200,
            quote_timestamp=AS_OF,
            quote_age_seconds=0.0,
            provider_contract_id=f"NIFTY26O06{int(strike)}{side}",
            quality=DataQualityStatus.OK,
            lot_size=lot_size,
            expiry_class="WEEKLY",
            is_fixture=False,
        )
        for strike in (spot - 50, spot, spot + 50)
        for side in ("CE", "PE")
    )
    return build_fixture_snapshot(
        underlying="NIFTY",
        as_of=AS_OF,
        spot=spot,
        option_contracts=quotes,
        diagnostics=history_diagnostics_from_closes(
            closes,
            interval="M15",
            earliest="2026-09-23T09:15:00+05:30",
            latest="2026-09-24T11:45:00+05:30",
            provenance="LIVE",
        ),
    )


class RiskUniverseAllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = campaign_paper_config(load_config(environ={"GROW_EXECUTION_MODE": "paper"}))
        self.clock = FrozenClock(AS_OF)
        self.guard = make_guard(self.config, clock=self.clock)

    def test_a_nifty_passes_universe_validation(self) -> None:
        self.assertIn("NIFTY", self.config.strategies.universe)
        self.assertNotIn("NIFTY", self.config.market.universe)
        self.assertIn("NIFTY", risk_allowed_tickers(self.config))
        p = _proposal("NIFTY", price=20.0, qty=1)
        brief = _brief(Symbol("NIFTY"), price=20.0)
        verdict = self.guard.evaluate(
            p, brief, cash=10_000, gross_notional=0, daily_pnl=0, symbol_notional=0, open_positions=0
        )
        universe = [r for r in verdict.rule_results if r[0] == "symbol.universe"][0]
        self.assertTrue(universe[1], universe[2])
        self.assertIn("listed=True", universe[2])

    def test_b_unknown_symbol_still_fails(self) -> None:
        p = _proposal("NOTAREAL", price=20.0, qty=1)
        verdict = self.guard.evaluate(
            p,
            _brief(Symbol("NOTAREAL"), 20.0),
            cash=10_000,
            gross_notional=0,
            daily_pnl=0,
            symbol_notional=0,
        )
        self.assertFalse(verdict.approved)
        self.assertIn("universe", verdict.reason)
        self.assertIn("listed=False", verdict.reason)

    def test_c_unconfigured_underlying_fails(self) -> None:
        # BANKNIFTY is allowed by config schema but not in default strategies.universe.
        self.assertNotIn("BANKNIFTY", self.config.strategies.universe)
        self.assertNotIn("BANKNIFTY", risk_allowed_tickers(self.config))
        p = _proposal("BANKNIFTY", price=20.0, qty=1)
        verdict = self.guard.evaluate(
            p,
            _brief(Symbol("BANKNIFTY"), 20.0),
            cash=10_000,
            gross_notional=0,
            daily_pnl=0,
            symbol_notional=0,
        )
        self.assertFalse(verdict.approved)
        self.assertIn("universe", verdict.reason)

    def test_d_risk_limits_unchanged(self) -> None:
        self.assertEqual(self.config.risk.max_per_trade_risk, 1_000)
        self.assertEqual(self.config.risk.max_daily_loss, 2_000)
        self.assertEqual(self.config.risk.max_open_positions, 2)
        self.assertEqual(self.config.paper.starting_cash, 10_000)

    def test_e_no_broker_and_live_lock(self) -> None:
        self.assertIs(LIVE_TRADING_COMPILED, False)
        p = _proposal("NIFTY", price=20.0, qty=1)
        self.assertEqual(p.venue, Venue.PAPER)

    def test_sensex_in_strategy_allowlist(self) -> None:
        self.assertIn("SENSEX", self.config.strategies.universe)
        self.assertIn("SENSEX", risk_allowed_tickers(self.config))
        p = _proposal("SENSEX", price=20.0, qty=1)
        verdict = self.guard.evaluate(
            p,
            _brief(Symbol("SENSEX"), 20.0),
            cash=10_000,
            gross_notional=0,
            daily_pnl=0,
            symbol_notional=0,
        )
        universe = [r for r in verdict.rule_results if r[0] == "symbol.universe"][0]
        self.assertTrue(universe[1], universe[2])


class NiftyCampaignUniverseIntegrationTests(unittest.TestCase):
    def test_nifty_campaign_candidate_passes_risk_universe(self) -> None:
        config = campaign_paper_config(load_config(environ={"GROW_EXECUTION_MODE": "paper"}))
        # Keep premium tiny so cash/concentration/per-trade risk are not the first failure.
        config = replace(
            config,
            risk=replace(
                config.risk,
                max_symbol_concentration=1.0,
                max_per_trade_risk=50_000,
                max_position_notional=100_000,
            ),
        )
        clock = FrozenClock(AS_OF)
        runner = CampaignRunner(
            config,
            clock=clock,
            risk_secret=TEST_RISK_SECRET,
            specialists=(CampaignOptionsAgent(config),),
            apply_campaign_defaults=False,
        )
        snap = _nifty_option_snap(premium=15.0, lot_size=65)
        agent = CampaignOptionsAgent(config).analyze(snap, cycle_id="pre")
        self.assertEqual(agent.candidate_action, CandidateAction.PAPER_OPEN)
        self.assertEqual(agent.calculated_metrics.get("underlying"), "NIFTY")

        result = runner.run_cycle(snap, cycle_id="nifty-universe")
        self.assertNotIn(
            "symbol.universe",
            " ".join(result.decision.reason_codes),
            msg=list(result.decision.reason_codes),
        )
        universe_gate = [g for g in (result.decision.gate_results or ()) if g[0] == "risk_guard"]
        self.assertEqual(result.decision.risk_guard_result, "APPROVED", msg=result.decision.reason_codes)
        self.assertTrue(result.execution.accepted, msg=result.execution.reason)
        self.assertEqual(result.execution.reason, "FILLED")
        self.assertEqual(result.execution.broker_order_calls, 0)
        self.assertIs(LIVE_TRADING_COMPILED, False)


if __name__ == "__main__":
    unittest.main()
