from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta
from dataclasses import replace

from grow.clock import IST
from grow.config import load_config
from grow.data.schema import FIXTURE_SOURCE, Bar, BarSeries, MarketSnapshot, SnapshotQuality, Timeframe
from grow.errors import GrowConfigError, GrowInterfaceNotImplemented
from grow.options import IndexOptionsEngine, OptionsDesk, open_option_source
from grow.options.engine import IndexOptionsEngine as Engine
from grow.options.fixture import FixtureOptionChain
from grow.options.models import (
    DecisionStatus,
    ExpiryClass,
    FieldSource,
    OptionChainSnapshot,
    OptionContract,
    OptionExpiry,
    OptionType,
)
from grow.options.score import WEIGHTS, rank_key
from grow.options.select import atm_strike, choose_expiry, strike_step, strike_window
from grow.options.source import OptionChainSource
from grow.options.validate import intrinsic
from grow.strategies.signal import StrategySignal
from grow.types import SessionState, Symbol


def _bar(symbol: Symbol, start: datetime, price: float) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe=Timeframe.M15,
        start=start,
        end=start + timedelta(minutes=15),
        open=price - 1,
        high=price + 2,
        low=price - 2,
        close=price,
        volume=1000,
    )


def _snapshot(ticker: str, as_of: datetime, spot: float) -> MarketSnapshot:
    symbol = Symbol(ticker)
    start = as_of - timedelta(minutes=15)
    series = BarSeries(symbol=symbol, timeframe=Timeframe.M15, bars=(_bar(symbol, start, spot),))
    return MarketSnapshot(
        snapshot_id="und-1",
        symbol=symbol,
        as_of=as_of,
        session=SessionState.OPEN,
        last_price=spot,
        currency="INR",
        series={Timeframe.M15: series},
        quality=SnapshotQuality(
            complete=True,
            stale=False,
            missing_count=0,
            expected_count=1,
            last_bar_end=as_of,
            notes=(),
        ),
        source=FIXTURE_SOURCE,
    )


def _signal(ticker: str, direction: str, as_of: datetime, snapshot_id: str = "und-1") -> StrategySignal:
    return StrategySignal(
        symbol=Symbol(ticker),
        strategy="ema_trend",
        direction=direction,
        entry=100.0,
        stop=90.0 if direction == "BULLISH" else 110.0,
        target=120.0 if direction == "BULLISH" else 80.0,
        confidence=0.6,
        timeframe=Timeframe.M15,
        reason="test",
        as_of=as_of,
        snapshot_id=snapshot_id,
        signal_id="sig-1",
        strategy_version="v1",
    )


class OptionsEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.engine = IndexOptionsEngine(self.config)
        self.as_of = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
        self.spot = 25000.0
        self.source = FixtureOptionChain()
        self.chain = self.source.snapshot("NIFTY", self.as_of, spot=self.spot)
        self.snap = _snapshot("NIFTY", self.as_of, self.spot)

    def test_desk_still_refuses_vendor_and_selling(self) -> None:
        with self.assertRaises(GrowInterfaceNotImplemented):
            OptionsDesk().chain("NIFTY")
        with self.assertRaises(GrowInterfaceNotImplemented):
            OptionsDesk().sell()
        with self.assertRaises(GrowInterfaceNotImplemented):
            OptionsDesk().buy()

    def test_source_is_protocol_and_offline(self) -> None:
        self.assertIsInstance(self.source, OptionChainSource)
        self.assertTrue(self.chain.is_fixture)
        self.assertFalse(self.source.meta().is_live)
        src = inspect.getsource(Engine)
        self.assertNotIn("def place_order", src)
        self.assertNotIn("def execute", src)
        self.assertNotIn("import grow.paper", src)

    def test_config_defaults(self) -> None:
        opt = self.config.options
        self.assertEqual(opt.provider, "fixture")
        self.assertFalse(opt.allow_same_day)
        self.assertEqual(opt.preferred_expiry_class, "weekly")
        self.assertEqual(opt.max_distance_from_atm, 2)
        self.assertFalse(opt.allow_live_chain)
        self.assertFalse(self.config.data.allow_options_chain)

    def test_bullish_maps_to_buy_ce(self) -> None:
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, self.chain)
        self.assertEqual(decision.status, DecisionStatus.CANDIDATE)
        assert decision.candidate is not None
        self.assertEqual(decision.candidate.option_type, "CE")
        self.assertEqual(decision.candidate.intent, "BUY")
        self.assertEqual(decision.candidate.direction, "BULLISH")
        self.assertEqual(decision.candidate.to_dict()["display"], "BUY CE")
        self.assertFalse(decision.candidate.to_dict()["executed"])

    def test_bearish_maps_to_buy_pe(self) -> None:
        decision = self.engine.evaluate(_signal("NIFTY", "BEARISH", self.as_of), self.snap, self.chain)
        self.assertEqual(decision.status, DecisionStatus.CANDIDATE)
        assert decision.candidate is not None
        self.assertEqual(decision.candidate.option_type, "PE")
        self.assertEqual(decision.candidate.intent, "BUY")

    def test_bullish_pe_rejected(self) -> None:
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, self.chain)
        assert decision.candidate is not None
        self.assertEqual(decision.candidate.option_type, "CE")
        self.assertTrue(any(r.reason == "DIRECTION_GATE:CE" for r in decision.rejected))
        self.assertTrue(any(r.identity[3] == "PE" and r.reason == "DIRECTION_GATE:CE" for r in decision.rejected))

    def test_bearish_ce_rejected(self) -> None:
        decision = self.engine.evaluate(_signal("NIFTY", "BEARISH", self.as_of), self.snap, self.chain)
        assert decision.candidate is not None
        self.assertEqual(decision.candidate.option_type, "PE")
        self.assertTrue(any(r.reason == "DIRECTION_GATE:PE" for r in decision.rejected))
        self.assertTrue(any(r.identity[3] == "CE" and r.reason == "DIRECTION_GATE:PE" for r in decision.rejected))

    def test_selling_intent_rejected(self) -> None:
        for side in ("SHORT", "SELL", "OPTION_SELL", "LONG", "BUY"):
            decision = self.engine.evaluate(_signal("NIFTY", side, self.as_of), self.snap, self.chain)
            self.assertEqual(decision.status, DecisionStatus.NO_TRADE)
            self.assertTrue(any("EXECUTION_LANGUAGE" in d for d in decision.diagnostics))

    def test_unsupported_and_stock(self) -> None:
        snap = _snapshot("RELIANCE", self.as_of, 1400)
        chain = replace(self.chain, underlying="RELIANCE")
        decision = self.engine.evaluate(_signal("RELIANCE", "BULLISH", self.as_of), snap, chain)
        self.assertEqual(decision.status, DecisionStatus.NO_TRADE)

    def test_banknifty_fixture(self) -> None:
        as_of = self.as_of
        spot = 55000.0
        chain = self.source.snapshot("BANKNIFTY", as_of, spot=spot)
        snap = _snapshot("BANKNIFTY", as_of, spot)
        decision = self.engine.evaluate(_signal("BANKNIFTY", "BULLISH", as_of), snap, chain)
        self.assertEqual(decision.status, DecisionStatus.CANDIDATE)
        assert decision.candidate is not None
        self.assertEqual(decision.candidate.underlying, "BANKNIFTY")

    def test_expiry_is_nearest_weekly_not_same_day(self) -> None:
        expiry, why = choose_expiry(self.chain, self.as_of, self.config.options)
        self.assertIsNotNone(expiry)
        assert expiry is not None
        self.assertEqual(expiry.klass, ExpiryClass.WEEKLY)
        self.assertGreater(expiry.day, self.as_of.date())
        self.assertIn("EXPIRY", why)
        tuesday = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
        chain = self.source.snapshot("NIFTY", tuesday, spot=self.spot)
        chosen, _ = choose_expiry(chain, tuesday, self.config.options)
        assert chosen is not None
        self.assertGreater(chosen.day, tuesday.date())

    def test_atm_window(self) -> None:
        step = strike_step(tuple(c.strike for c in self.chain.contracts))
        self.assertEqual(step, 50.0)
        window = strike_window(self.spot, tuple(c.strike for c in self.chain.contracts), 2)
        atm = atm_strike(self.spot, window)
        self.assertIsNotNone(atm)
        self.assertEqual(len(window), 5)
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, self.chain)
        assert decision.candidate is not None
        self.assertIn(decision.candidate.strike, window)

    def test_crossed_quote_rejected(self) -> None:
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, self.chain)
        self.assertTrue(any(r.reason == "CROSSED_QUOTE" for r in decision.rejected))

    def test_stale_chain_no_trade(self) -> None:
        old = replace(self.chain, as_of=self.as_of - timedelta(minutes=30))
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, old)
        self.assertEqual(decision.status, DecisionStatus.NO_TRADE)
        self.assertTrue(any("STALE" in d for d in decision.diagnostics))

    def test_future_quote_rejected(self) -> None:
        future = replace(self.chain.contracts[0], timestamp=self.as_of + timedelta(hours=1))
        chain = replace(self.chain, contracts=(future,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "FUTURE_QUOTE" for r in decision.rejected))

    def test_expired_rejected(self) -> None:
        expired = replace(self.chain.contracts[0], expiry=self.as_of.date() - timedelta(days=1))
        chain = replace(self.chain, contracts=(expired,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "EXPIRED" for r in decision.rejected))

    def test_negative_oi_volume(self) -> None:
        bad_oi = replace(self.chain.contracts[0], open_interest=-1)
        chain = replace(self.chain, contracts=(bad_oi,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "NEGATIVE_OI" for r in decision.rejected))
        bad_vol = replace(self.chain.contracts[1], volume=-5)
        chain = replace(self.chain, contracts=(bad_vol,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "NEGATIVE_VOLUME" for r in decision.rejected))

    def test_ask_below_bid(self) -> None:
        crossed = replace(self.chain.contracts[0], bid=20.0, ask=10.0, last_price=15.0, volume=2000, open_interest=2000)
        chain = replace(self.chain, contracts=(crossed,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "CROSSED_QUOTE" for r in decision.rejected))

    def test_intrinsic_extrinsic(self) -> None:
        self.assertEqual(intrinsic(OptionType.CE, 25000, 24900), 100)
        self.assertEqual(intrinsic(OptionType.PE, 25000, 25100), 100)
        self.assertEqual(intrinsic(OptionType.CE, 25000, 25100), 0)
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, self.chain)
        assert decision.candidate is not None
        self.assertGreaterEqual(decision.candidate.extrinsic_value, -0.05)

    def test_missing_iv_still_eligible(self) -> None:
        liquid = [
            replace(c, implied_volatility=None, delta=None, iv_source=FieldSource.UNAVAILABLE, greek_source=FieldSource.UNAVAILABLE)
            for c in self.chain.contracts
            if c.volume >= 100 and c.open_interest >= 500
        ]
        chain = replace(self.chain, contracts=tuple(liquid))
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertEqual(decision.status, DecisionStatus.CANDIDATE)
        assert decision.candidate is not None
        self.assertIsNone(decision.candidate.implied_volatility)
        self.assertEqual(decision.candidate.score.components["iv_suitability"], 0.0)
        self.assertEqual(decision.candidate.score.components["greek_suitability"], 0.0)

    def test_determinism_and_ids(self) -> None:
        sig = _signal("NIFTY", "BULLISH", self.as_of)
        a = self.engine.evaluate(sig, self.snap, self.chain)
        b = self.engine.evaluate(sig, self.snap, self.chain)
        self.assertEqual(a.to_dict(), b.to_dict())
        assert a.candidate is not None
        self.assertEqual(len(a.candidate.candidate_id), 16)

    def test_look_ahead_chain_does_not_change_past(self) -> None:
        sig = _signal("NIFTY", "BULLISH", self.as_of)
        first = self.engine.evaluate(sig, self.snap, self.chain)
        later_as_of = self.as_of + timedelta(hours=3)
        later_chain = self.source.snapshot("NIFTY", later_as_of, spot=self.spot + 400)
        again = self.engine.evaluate(sig, self.snap, self.chain)
        self.assertEqual(first.to_dict(), again.to_dict())
        later_snap = replace(self.snap, as_of=later_as_of, last_price=self.spot + 400, snapshot_id="und-2")
        later_sig = _signal("NIFTY", "BULLISH", later_as_of, snapshot_id="und-2")
        later = self.engine.evaluate(later_sig, later_snap, later_chain)
        self.assertNotEqual(first.candidate and first.candidate.candidate_id, later.candidate and later.candidate.candidate_id)

    def test_live_chain_flag_refused(self) -> None:
        live = replace(self.chain, source_id="zerodha-live", is_fixture=False)
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, live)
        self.assertEqual(decision.status, DecisionStatus.NO_TRADE)
        self.assertIn("LIVE_CHAIN_FORBIDDEN", decision.diagnostics)
        object.__setattr__(self.config.options, "allow_live_chain", True)
        with self.assertRaises(GrowConfigError):
            self.config.assert_safe()

    def test_fixture_provider_rejects_non_fixture_chain(self) -> None:
        chain = replace(self.chain, is_fixture=False, source_id="t")
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertEqual(decision.status, DecisionStatus.NO_TRADE)
        self.assertIn("LIVE_CHAIN_FORBIDDEN", decision.diagnostics)

    def test_historical_provider_accepts_non_fixture_chain(self) -> None:
        cfg = replace(self.config, options=replace(self.config.options, provider="historical", allow_live_chain=False))
        engine = IndexOptionsEngine(cfg)
        chain = replace(self.chain, is_fixture=False, source_id="t")
        decision = engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertNotIn("LIVE_CHAIN_FORBIDDEN", decision.diagnostics)
        live = replace(chain, source_id="vendor-live")
        blocked = engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, live)
        self.assertIn("LIVE_CHAIN_FORBIDDEN", blocked.diagnostics)

    def test_no_eligible_expiry_no_trade(self) -> None:
        chain = replace(
            self.chain,
            expiries=(OptionExpiry(self.as_of.date(), ExpiryClass.WEEKLY),),
            contracts=tuple(c for c in self.chain.contracts if c.expiry == self.as_of.date()),
        )
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertEqual(decision.status, DecisionStatus.NO_TRADE)
        self.assertTrue(any("NO_ELIGIBLE_EXPIRY" in d for d in decision.diagnostics))

    def test_open_option_source(self) -> None:
        src = open_option_source()
        self.assertIsInstance(src, FixtureOptionChain)

    def test_chain_spot_mismatch(self) -> None:
        chain = replace(self.chain, spot=self.spot + 25)
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertEqual(decision.status, DecisionStatus.NO_TRADE)
        self.assertIn("CHAIN_SPOT_MISMATCH", decision.diagnostics)

    def test_stale_quote(self) -> None:
        stale = replace(self.chain.contracts[0], timestamp=self.as_of - timedelta(minutes=20))
        chain = replace(self.chain, contracts=(stale,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "STALE_QUOTE" for r in decision.rejected))

    def test_duplicate_contract(self) -> None:
        first = self.chain.contracts[0]
        chain = replace(self.chain, contracts=(first, first) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "DUPLICATE_CONTRACT" for r in decision.rejected))

    def test_negative_bid_and_ask(self) -> None:
        bad_bid = replace(self.chain.contracts[0], bid=-1.0, ask=10.0, last_price=5.0)
        chain = replace(self.chain, contracts=(bad_bid,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "NEGATIVE_BID" for r in decision.rejected))
        bad_ask = replace(self.chain.contracts[1], bid=1.0, ask=-2.0, last_price=5.0)
        chain = replace(self.chain, contracts=(bad_ask,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "NEGATIVE_ASK" for r in decision.rejected))

    def test_invalid_strike(self) -> None:
        bad = replace(self.chain.contracts[0], strike=0)
        chain = replace(self.chain, contracts=(bad,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "INVALID_STRIKE" for r in decision.rejected))

    def test_missing_premium(self) -> None:
        bad = replace(self.chain.contracts[0], bid=None, ask=None, last_price=None)
        chain = replace(self.chain, contracts=(bad,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "NO_PREMIUM" for r in decision.rejected))

    def test_iv_out_of_range_and_source_missing(self) -> None:
        high = replace(self.chain.contracts[0], implied_volatility=3.5, iv_source=FieldSource.PROVIDER)
        chain = replace(self.chain, contracts=(high,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "IV_OUT_OF_BOUNDS" for r in decision.rejected))
        orphan = replace(
            self.chain.contracts[1],
            implied_volatility=0.2,
            iv_source=FieldSource.UNAVAILABLE,
        )
        chain = replace(self.chain, contracts=(orphan,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "IV_SOURCE_MISSING" for r in decision.rejected))
        greek = replace(
            self.chain.contracts[2],
            delta=0.4,
            greek_source=FieldSource.UNAVAILABLE,
        )
        chain = replace(self.chain, contracts=(greek,) + self.chain.contracts[1:])
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertTrue(any(r.reason == "GREEK_SOURCE_MISSING" for r in decision.rejected))

    def test_no_permitted_strike(self) -> None:
        only_pe = tuple(c for c in self.chain.contracts if c.option_type is OptionType.PE)
        chain = replace(self.chain, contracts=only_pe)
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertEqual(decision.status, DecisionStatus.NO_TRADE)
        self.assertTrue(any("NO_PERMITTED_STRIKE" in d for d in decision.diagnostics))

    def test_no_liquid_contract(self) -> None:
        dry = tuple(replace(c, volume=1, open_interest=1) for c in self.chain.contracts)
        chain = replace(self.chain, contracts=dry)
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertEqual(decision.status, DecisionStatus.NO_TRADE)
        self.assertTrue(any("NO_LIQUID_CONTRACT" in d for d in decision.diagnostics))

    def test_score_bounds_and_weight_sum(self) -> None:
        self.assertAlmostEqual(sum(WEIGHTS.values()), 1.0, places=9)
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, self.chain)
        assert decision.candidate is not None
        for name, value in decision.candidate.score.components.items():
            self.assertGreaterEqual(value, 0.0, name)
            self.assertLessEqual(value, 1.0, name)
        self.assertAlmostEqual(sum(decision.candidate.score.weights.values()), 1.0, places=9)

    def test_equal_score_tie_break(self) -> None:
        ident_lo = ("NIFTY", "2026-09-22", 25000.0, "CE")
        ident_hi = ("NIFTY", "2026-09-22", 25050.0, "CE")
        equal = [
            rank_key(0.50, 1000, 500, 0.02, ident_hi),
            rank_key(0.50, 1000, 500, 0.02, ident_lo),
        ]
        equal.sort()
        self.assertEqual(equal[0][-1], ident_lo)
        by_oi = [
            rank_key(0.50, 1000, 500, 0.02, ident_lo),
            rank_key(0.50, 2000, 500, 0.02, ident_hi),
        ]
        by_oi.sort()
        self.assertEqual(by_oi[0][-1], ident_hi)
        by_spread = [
            rank_key(0.50, 1000, 500, 0.04, ident_lo),
            rank_key(0.50, 1000, 500, 0.01, ident_hi),
        ]
        by_spread.sort()
        self.assertEqual(by_spread[0][-1], ident_hi)

    def test_engine_tie_break_identity_asc(self) -> None:
        weekly = _next_weekly(self.as_of.date())
        strikes = (24900.0, 24950.0, 25000.0, 25050.0, 25100.0)
        contracts = []
        for strike in strikes:
            contracts.append(
                _liquid_contract("NIFTY", weekly, strike, OptionType.PE, self.as_of)
            )
        contracts.append(_liquid_contract("NIFTY", weekly, 25100.0, OptionType.CE, self.as_of))
        contracts.append(_liquid_contract("NIFTY", weekly, 25050.0, OptionType.CE, self.as_of))
        chain = OptionChainSnapshot(
            snapshot_id="tie-1",
            underlying="NIFTY",
            as_of=self.as_of,
            spot=self.spot,
            expiries=(OptionExpiry(weekly, ExpiryClass.WEEKLY),),
            contracts=tuple(contracts),
            source_id="grow.options.fixture.v1",
            is_fixture=True,
            provider_metadata={},
        )
        decision = self.engine.evaluate(_signal("NIFTY", "BULLISH", self.as_of), self.snap, chain)
        self.assertEqual(decision.status, DecisionStatus.CANDIDATE)
        assert decision.candidate is not None
        self.assertEqual(decision.candidate.option_type, "CE")
        self.assertEqual(decision.candidate.strike, 25050.0)
        self.assertEqual(decision.candidate.volume, 2500)
        self.assertEqual(decision.candidate.open_interest, 8000)


def _next_weekly(day):
    from datetime import timedelta as _td

    probe = day + _td(days=1)
    while probe.weekday() != 1:
        probe += _td(days=1)
    return probe


def _liquid_contract(underlying, expiry, strike, option_type, as_of) -> OptionContract:
    return OptionContract(
        underlying=underlying,
        expiry=expiry,
        expiry_class=ExpiryClass.WEEKLY,
        strike=strike,
        option_type=option_type,
        bid=10.0,
        ask=10.40,
        last_price=10.20,
        volume=2500,
        open_interest=8000,
        previous_open_interest=None,
        implied_volatility=None,
        delta=None,
        gamma=None,
        theta=None,
        vega=None,
        timestamp=as_of,
        provider_contract_id=f"{underlying}-{expiry}-{strike}-{option_type.value}",
        iv_source=FieldSource.UNAVAILABLE,
        greek_source=FieldSource.UNAVAILABLE,
    )


if __name__ == "__main__":
    unittest.main()
