from __future__ import annotations

import unittest

from grow.strategies.confidence import clamp, regime_alignment, score_confidence, unit
from grow.strategies.models import Direction, MarketRegime
from grow.strategies.strategies.breakout import BreakoutStrategy
from grow.strategies.strategies.mean_reversion import MeanReversionStrategy
from grow.strategies.strategies.momentum import MomentumStrategy
from grow.strategies.strategies.trend import EmaTrendStrategy
from tests.test_strategy_directions import _ctx, _ind, _snapshot


class ScoreFormulaTests(unittest.TestCase):
    def test_equal_weight_mean_and_clamp(self) -> None:
        self.assertEqual(score_confidence({"a": 0.0, "b": 1.0, "c": 0.5, "d": 0.5}), 0.5)
        self.assertEqual(score_confidence({"a": 2.0, "b": 2.0}), 1.0)
        self.assertEqual(score_confidence({"a": -1.0}), 0.0)
        self.assertEqual(score_confidence({}), 0.0)
        self.assertEqual(clamp(1.2), 1.0)
        self.assertEqual(clamp(-0.1), 0.0)

    def test_unit_is_symmetric(self) -> None:
        self.assertEqual(unit(3, 10), unit(-3, 10))
        self.assertEqual(unit(10, 10), 1.0)
        self.assertEqual(unit(20, 10), 1.0)
        self.assertEqual(unit(0, 10), 0.0)

    def test_regime_tables_are_mirrors_for_trend_style(self) -> None:
        for regime in MarketRegime:
            bull = regime_alignment(Direction.BULLISH, regime, style="trend")
            if regime is MarketRegime.BULL_TREND:
                bear_of = regime_alignment(Direction.BEARISH, MarketRegime.BEAR_TREND, style="trend")
                self.assertEqual(bull, bear_of)
            self.assertGreaterEqual(bull, 0.0)
            self.assertLessEqual(bull, 1.0)


class StrategyConfidenceTests(unittest.TestCase):
    def test_same_input_same_confidence(self) -> None:
        strat = EmaTrendStrategy()
        snap = _snapshot(110)
        ctx = _ctx(
            _ind(close=110, ema_fast=108, ema_slow=100, ema_fast_slope=0.4, atr=1.0),
            MarketRegime.BULL_TREND,
        )
        a = strat.evaluate(snap, ctx)
        b = strat.evaluate(snap, ctx)
        self.assertIsNotNone(a)
        assert a is not None and b is not None
        self.assertEqual(a.confidence, b.confidence)
        self.assertEqual(a.extras, b.extras)

    def test_stronger_ema_gap_scores_higher(self) -> None:
        strat = EmaTrendStrategy()
        snap = _snapshot(110)
        weak = strat.evaluate(
            snap,
            _ctx(
                _ind(close=110, ema_fast=109.5, ema_slow=109.0, ema_fast_slope=0.05, atr=1.0),
                MarketRegime.UNKNOWN,
            ),
        )
        strong = strat.evaluate(
            snap,
            _ctx(
                _ind(close=110, ema_fast=108, ema_slow=90, ema_fast_slope=0.8, atr=1.0),
                MarketRegime.BULL_TREND,
            ),
        )
        self.assertIsNotNone(weak)
        self.assertIsNotNone(strong)
        assert weak is not None and strong is not None
        self.assertGreater(strong.confidence, weak.confidence)

    def test_bullish_and_bearish_use_the_same_formula(self) -> None:
        mirrored = {
            "trend_alignment": unit(8, 1.5),
            "indicator_strength": unit(0.4, 0.05),
            "structure_confirmation": unit(2, 1.1),
            "regime_alignment": 1.0,
        }
        flipped = {
            "trend_alignment": unit(-8, 1.5),
            "indicator_strength": unit(-0.4, 0.05),
            "structure_confirmation": unit(-2, 1.1),
            "regime_alignment": 1.0,
        }
        self.assertEqual(score_confidence(mirrored), score_confidence(flipped))
        strat = EmaTrendStrategy()
        snap = _snapshot(100)
        bull = strat.evaluate(
            snap,
            _ctx(
                _ind(close=110, ema_fast=108, ema_slow=100, ema_fast_slope=0.4, atr=1.0),
                MarketRegime.BULL_TREND,
            ),
        )
        bear = strat.evaluate(
            snap,
            _ctx(
                _ind(close=90, ema_fast=92, ema_slow=100, ema_fast_slope=-0.4, atr=1.0),
                MarketRegime.BEAR_TREND,
            ),
        )
        self.assertIsNotNone(bull)
        self.assertIsNotNone(bear)
        assert bull is not None and bear is not None
        self.assertEqual(bull.direction, "BULLISH")
        self.assertEqual(bear.direction, "BEARISH")
        self.assertGreaterEqual(bull.confidence, 0.0)
        self.assertGreaterEqual(bear.confidence, 0.0)

    def test_all_strategies_bound_and_vary(self) -> None:
        snap = _snapshot()
        cases = [
            (
                EmaTrendStrategy(),
                _ind(close=110, ema_fast=108, ema_slow=100, ema_fast_slope=0.4, atr=1.0),
                MarketRegime.BULL_TREND,
            ),
            (
                MomentumStrategy(),
                _ind(close=110, ema_fast=100, rsi=70, roc=1.2, atr=1.0),
                MarketRegime.BULL_TREND,
            ),
            (
                BreakoutStrategy(),
                _ind(close=110, rolling_high=105, rolling_low=90, body=2.0, range=3.0, atr=1.0),
                MarketRegime.BULL_TREND,
            ),
            (
                MeanReversionStrategy(),
                _ind(close=90, ema_fast=100, rsi=20, atr=1.0),
                MarketRegime.RANGE,
            ),
        ]
        seen = []
        for strat, ind, regime in cases:
            signal = strat.evaluate(snap, _ctx(ind, regime))
            self.assertIsNotNone(signal, strat.name)
            assert signal is not None
            self.assertGreaterEqual(signal.confidence, 0.0)
            self.assertLessEqual(signal.confidence, 1.0)
            self.assertIn("confidence=", signal.reason)
            self.assertIn("confidence_parts", signal.extras or {})
            seen.append(signal.confidence)
        self.assertGreater(len(set(seen)), 1)

    def test_confidence_uses_only_as_of_indicators(self) -> None:
        """Later bars cannot change a signal that was scored on earlier indicators."""
        strat = EmaTrendStrategy()
        snap = _snapshot(110)
        early = _ctx(
            _ind(close=110, ema_fast=109.8, ema_slow=109.5, ema_fast_slope=0.02, atr=1.0, bar_count=40),
            MarketRegime.UNKNOWN,
        )
        first = strat.evaluate(snap, early)
        # A later, stronger tape exists only as a different context.
        later = _ctx(
            _ind(close=130, ema_fast=120, ema_slow=90, ema_fast_slope=1.5, atr=1.0, bar_count=80),
            MarketRegime.BULL_TREND,
        )
        future = strat.evaluate(snap, later)
        again = strat.evaluate(snap, early)
        self.assertEqual(first and first.confidence, again and again.confidence)
        self.assertNotEqual(first and first.confidence, future and future.confidence)

    def test_momentum_rsi_strength_monotonic(self) -> None:
        strat = MomentumStrategy()
        snap = _snapshot()
        mild = strat.evaluate(
            snap,
            _ctx(_ind(close=101, ema_fast=100, rsi=56, roc=0.1, atr=1.0), MarketRegime.UNKNOWN),
        )
        hot = strat.evaluate(
            snap,
            _ctx(_ind(close=110, ema_fast=100, rsi=85, roc=3.0, atr=1.0), MarketRegime.BULL_TREND),
        )
        self.assertIsNotNone(mild)
        self.assertIsNotNone(hot)
        assert mild is not None and hot is not None
        self.assertGreater(hot.confidence, mild.confidence)
