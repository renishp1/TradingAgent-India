from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from grow.clock import IST
from grow.config import load_config
from grow.data.schema import FIXTURE_SOURCE, Bar, BarSeries, MarketSnapshot, SnapshotQuality, Timeframe
from grow.errors import GrowConfigError
from grow.strategies.emit import atr_levels
from grow.strategies.models import Direction, IndicatorSnapshot, MarketRegime, RegimeSnapshot, StrategyContext, signal_fingerprint
from grow.strategies.regime import classify
from grow.strategies.signal import StrategySignal
from grow.strategies.strategies.breakout import BreakoutStrategy
from grow.strategies.strategies.mean_reversion import MeanReversionStrategy
from grow.strategies.strategies.momentum import MomentumStrategy
from grow.strategies.strategies.trend import EmaTrendStrategy
from grow.strategies.validate import validate_signal
from grow.types import SessionState, Symbol


def _ind(**overrides: object) -> IndicatorSnapshot:
    payload = dict(
        timeframe=Timeframe.M15,
        close=100.0,
        sma_fast=100.0,
        ema_fast=100.0,
        ema_slow=100.0,
        ema_fast_slope=0.0,
        rsi=50.0,
        roc=0.0,
        atr=1.0,
        rolling_std=0.5,
        prev_high=101.0,
        prev_low=99.0,
        rolling_high=102.0,
        rolling_low=98.0,
        range=2.0,
        body=0.4,
        upper_wick=0.2,
        lower_wick=0.2,
        bar_count=80,
    )
    payload.update(overrides)
    return IndicatorSnapshot(**payload)  # type: ignore[arg-type]


def _regime(label: MarketRegime) -> RegimeSnapshot:
    return RegimeSnapshot(label=label, d1_trend="test", m15_trend="test", volatility="mid", reason="fixture")


def _snapshot(close: float = 100.0) -> MarketSnapshot:
    symbol = Symbol("NIFTY")
    start = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
    bar = Bar(
        symbol=symbol,
        timeframe=Timeframe.M15,
        start=start,
        end=start + timedelta(minutes=15),
        open=close - 0.4,
        high=close + 0.8,
        low=close - 0.8,
        close=close,
        volume=1000,
    )
    series = BarSeries(symbol=symbol, timeframe=Timeframe.M15, bars=(bar,))
    return MarketSnapshot(
        snapshot_id="snap-dir",
        symbol=symbol,
        as_of=bar.end,
        session=SessionState.OPEN,
        last_price=close,
        currency="INR",
        series={Timeframe.M15: series},
        quality=SnapshotQuality(
            complete=True,
            stale=False,
            missing_count=0,
            expected_count=1,
            last_bar_end=bar.end,
            notes=(),
        ),
        source=FIXTURE_SOURCE,
    )


def _ctx(indicators: IndicatorSnapshot, regime: MarketRegime, **params: object) -> StrategyContext:
    snap = _snapshot(indicators.close)
    return StrategyContext(
        as_of=snap.as_of,
        timeframe=Timeframe.M15,
        regime=_regime(regime),
        indicators=indicators,
        snapshot_id=snap.snapshot_id,
        session=SessionState.OPEN,
        params={"version": "v1", **params},
    )


def _d1(n: int, start_px: float, drift: float) -> tuple[Bar, ...]:
    symbol = Symbol("NIFTY")
    day = datetime(2026, 6, 1, 9, 15, tzinfo=IST)
    bars = []
    px = start_px
    for _ in range(n):
        bars.append(
            Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                start=day,
                end=day + timedelta(hours=6, minutes=15),
                open=round(px - 1, 2),
                high=round(px + 2, 2),
                low=round(px - 2, 2),
                close=round(px, 2),
                volume=1000,
            )
        )
        px += drift
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return tuple(bars)


class DirectionContractTests(unittest.TestCase):
    def test_research_directions_only(self) -> None:
        self.assertEqual({d.value for d in Direction}, {"BULLISH", "BEARISH"})

    def test_to_dict_rejects_execution_language(self) -> None:
        snap = _snapshot()
        for side in ("LONG", "SHORT", "SELL", "BUY"):
            sig = StrategySignal(
                symbol=snap.symbol,
                strategy="ema_trend",
                direction=side,
                entry=100,
                stop=99,
                target=102,
                confidence=0.5,
                timeframe=Timeframe.M15,
                reason="x",
                as_of=snap.as_of,
                snapshot_id=snap.snapshot_id,
                signal_id="deadbeef",
            )
            with self.assertRaises(ValueError):
                sig.to_dict()

    def test_validate_bullish_and_bearish_geometry(self) -> None:
        snap = _snapshot()
        bull = StrategySignal(
            symbol=snap.symbol,
            strategy="ema_trend",
            direction="BULLISH",
            entry=100,
            stop=99,
            target=102,
            confidence=0.5,
            timeframe=Timeframe.M15,
            reason="bull",
            as_of=snap.as_of,
            snapshot_id="snap-dir",
            signal_id="aaa",
            strategy_version="v1",
            risk_reward=2.0,
        )
        validate_signal(bull, snapshot_id="snap-dir", allowed_symbols=("NIFTY", "BANKNIFTY"))
        bear = StrategySignal(
            symbol=snap.symbol,
            strategy="ema_trend",
            direction="BEARISH",
            entry=100,
            stop=101,
            target=98,
            confidence=0.5,
            timeframe=Timeframe.M15,
            reason="bear",
            as_of=snap.as_of,
            snapshot_id="snap-dir",
            signal_id="bbb",
            strategy_version="v1",
            risk_reward=2.0,
        )
        validate_signal(bear, snapshot_id="snap-dir", allowed_symbols=("NIFTY", "BANKNIFTY"))
        bad = StrategySignal(
            symbol=snap.symbol,
            strategy="ema_trend",
            direction="BULLISH",
            entry=100,
            stop=101,
            target=102,
            confidence=0.5,
            timeframe=Timeframe.M15,
            reason="bad",
            as_of=snap.as_of,
            snapshot_id="snap-dir",
            signal_id="ccc",
            strategy_version="v1",
            risk_reward=2.0,
        )
        with self.assertRaises(GrowConfigError):
            validate_signal(bad, snapshot_id="snap-dir", allowed_symbols=("NIFTY", "BANKNIFTY"))

    def test_identity_includes_direction(self) -> None:
        common = dict(
            symbol="NSE:NIFTY",
            strategy="ema_trend",
            timeframe="M15",
            as_of="2026-09-21T11:15:00+05:30",
            snapshot_id="snap-dir",
            strategy_version="v1",
        )
        bull = signal_fingerprint(**common, direction="BULLISH")
        bear = signal_fingerprint(**common, direction="BEARISH")
        self.assertNotEqual(bull, bear)

    def test_primary_timeframe_locked(self) -> None:
        config = load_config()
        self.assertEqual(config.strategies.primary_timeframe, "M15")
        self.assertEqual(config.strategies.supported_timeframes, ("M5", "M15", "D1"))
        object.__setattr__(config.strategies, "primary_timeframe", "M5")
        with self.assertRaises(GrowConfigError):
            config.assert_safe()

    def test_atr_levels_flip_with_direction(self) -> None:
        stop, target = atr_levels(100, 1, Direction.BULLISH)
        self.assertLess(stop, 100)
        self.assertGreater(target, 100)
        stop, target = atr_levels(100, 1, Direction.BEARISH)
        self.assertGreater(stop, 100)
        self.assertLess(target, 100)


class StrategyDirectionTests(unittest.TestCase):
    def test_ema_bullish_and_bearish(self) -> None:
        strat = EmaTrendStrategy()
        snap = _snapshot(110)
        bull = strat.evaluate(
            snap,
            _ctx(
                _ind(close=110, ema_fast=108, ema_slow=100, ema_fast_slope=0.4, atr=1.0),
                MarketRegime.BULL_TREND,
            ),
        )
        self.assertIsNotNone(bull)
        assert bull is not None
        self.assertEqual(bull.direction, "BULLISH")
        self.assertLess(bull.stop, bull.entry)
        self.assertGreater(bull.target, bull.entry)
        bear = strat.evaluate(
            snap,
            _ctx(
                _ind(close=90, ema_fast=92, ema_slow=100, ema_fast_slope=-0.4, atr=1.0),
                MarketRegime.BEAR_TREND,
            ),
        )
        self.assertIsNotNone(bear)
        assert bear is not None
        self.assertEqual(bear.direction, "BEARISH")
        self.assertGreater(bear.stop, bear.entry)
        self.assertLess(bear.target, bear.entry)

    def test_momentum_bullish_and_bearish(self) -> None:
        strat = MomentumStrategy()
        snap = _snapshot()
        bull = strat.evaluate(
            snap,
            _ctx(_ind(close=110, ema_fast=100, rsi=70, roc=1.2, atr=1.0), MarketRegime.BULL_TREND),
        )
        bear = strat.evaluate(
            snap,
            _ctx(_ind(close=90, ema_fast=100, rsi=25, roc=-1.2, atr=1.0), MarketRegime.BEAR_TREND),
        )
        self.assertEqual(bull and bull.direction, "BULLISH")
        self.assertEqual(bear and bear.direction, "BEARISH")

    def test_breakout_and_breakdown(self) -> None:
        strat = BreakoutStrategy()
        snap = _snapshot()
        bull = strat.evaluate(
            snap,
            _ctx(
                _ind(close=110, rolling_high=105, rolling_low=90, body=2.0, atr=1.0),
                MarketRegime.BULL_TREND,
            ),
        )
        bear = strat.evaluate(
            snap,
            _ctx(
                _ind(close=85, rolling_high=100, rolling_low=90, body=-2.0, atr=1.0),
                MarketRegime.BEAR_TREND,
            ),
        )
        self.assertEqual(bull and bull.direction, "BULLISH")
        self.assertEqual(bear and bear.direction, "BEARISH")

    def test_mean_reversion_oversold_and_overbought(self) -> None:
        strat = MeanReversionStrategy()
        snap = _snapshot()
        bull = strat.evaluate(
            snap,
            _ctx(_ind(close=90, ema_fast=100, rsi=20, atr=1.0), MarketRegime.RANGE),
        )
        bear = strat.evaluate(
            snap,
            _ctx(_ind(close=110, ema_fast=100, rsi=80, atr=1.0), MarketRegime.RANGE),
        )
        self.assertEqual(bull and bull.direction, "BULLISH")
        self.assertEqual(bear and bear.direction, "BEARISH")

    def test_no_strategy_emits_sell_or_short(self) -> None:
        cases = [
            (EmaTrendStrategy(), _ind(close=90, ema_fast=92, ema_slow=100, ema_fast_slope=-0.4, atr=1.0), MarketRegime.BEAR_TREND),
            (MomentumStrategy(), _ind(close=90, ema_fast=100, rsi=25, roc=-1.2, atr=1.0), MarketRegime.BEAR_TREND),
            (BreakoutStrategy(), _ind(close=85, rolling_high=100, rolling_low=90, body=-2.0, atr=1.0), MarketRegime.BEAR_TREND),
            (MeanReversionStrategy(), _ind(close=110, ema_fast=100, rsi=80, atr=1.0), MarketRegime.RANGE),
        ]
        snap = _snapshot()
        for strat, ind, regime in cases:
            signal = strat.evaluate(snap, _ctx(ind, regime))
            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertNotIn(signal.direction, {"LONG", "SHORT", "SELL", "BUY"})
            self.assertEqual(signal.direction, "BEARISH")
            payload = signal.to_dict()
            self.assertEqual(payload["direction"], "BEARISH")
            self.assertNotIn("option", payload)
            self.assertNotIn("strike", payload)


class RegimeTests(unittest.TestCase):
    def test_all_six_labels(self) -> None:
        d1_up = _d1(60, 19000, 25)
        d1_down = _d1(60, 22000, -25)
        d1_flat = _d1(60, 20000, 0.1)
        cases = {
            MarketRegime.BULL_TREND: (
                _ind(close=110, ema_fast=108, ema_slow=100, atr=0.7),
                d1_up,
            ),
            MarketRegime.BEAR_TREND: (
                _ind(close=90, ema_fast=92, ema_slow=100, atr=0.7),
                d1_down,
            ),
            MarketRegime.HIGH_VOLATILITY: (
                _ind(close=100, ema_fast=101, ema_slow=100, atr=2.0),
                d1_flat,
            ),
            MarketRegime.LOW_VOLATILITY: (
                _ind(close=100, ema_fast=101, ema_slow=100, atr=0.2),
                d1_flat,
            ),
            MarketRegime.RANGE: (
                _ind(close=100, ema_fast=101, ema_slow=100, atr=0.7),
                d1_flat,
            ),
            MarketRegime.UNKNOWN: (
                _ind(close=110, ema_fast=108, ema_slow=100, atr=0.7),
                d1_down,
            ),
        }
        seen = {}
        for expected, (m15, d1) in cases.items():
            snap = classify(m15=m15, d1_bars=d1, fast_period=20, slow_period=50, atr_period=14)
            seen[expected] = snap.label
            self.assertEqual(snap.label, expected, msg=f"{expected} got {snap.label} ({snap.reason})")
        self.assertEqual(set(seen), set(MarketRegime))

    def test_insufficient_is_unknown(self) -> None:
        m15 = _ind(ema_fast=None, ema_slow=None, atr=None, close=100)
        snap = classify(m15=m15, d1_bars=(), fast_period=20, slow_period=50, atr_period=14)
        self.assertEqual(snap.label, MarketRegime.UNKNOWN)
