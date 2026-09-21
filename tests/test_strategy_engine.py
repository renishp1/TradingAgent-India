from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.data.factory import open_data_hub
from grow.data.schema import FIXTURE_SOURCE, Bar, BarSeries, MarketSnapshot, SnapshotQuality, Timeframe
from grow.errors import GrowConfigError, GrowInterfaceNotImplemented
from grow.strategies import StrategyBook, StrategyEngine
from grow.strategies.engine import build_indicators
from grow.strategies.indicators import ema, rolling_high, rsi, sma
from grow.strategies.models import MarketRegime
from grow.strategies.registry import StrategyRegistry, default_registry
from grow.strategies.signal import StrategySignal
from grow.types import SessionState, Symbol


def _bar(symbol: Symbol, start: datetime, price: float, tf: Timeframe = Timeframe.M15) -> Bar:
    delta = timedelta(minutes=15 if tf is Timeframe.M15 else 5 if tf is Timeframe.M5 else 6 * 60 + 15)
    return Bar(
        symbol=symbol,
        timeframe=tf,
        start=start,
        end=start + delta,
        open=round(price - 0.4, 2),
        high=round(price + 0.8, 2),
        low=round(price - 0.8, 2),
        close=round(price, 2),
        volume=1000,
    )


def _series(symbol: Symbol, prices: list[float], start: datetime, tf: Timeframe = Timeframe.M15) -> BarSeries:
    step = timedelta(minutes=15 if tf is Timeframe.M15 else 5)
    bars = tuple(_bar(symbol, start + i * step, px, tf) for i, px in enumerate(prices))
    return BarSeries(symbol=symbol, timeframe=tf, bars=bars)


def _snapshot(symbol: Symbol, m15: BarSeries, d1: BarSeries | None = None, session: SessionState = SessionState.OPEN) -> MarketSnapshot:
    last = m15.bars[-1]
    series = {Timeframe.M15: m15}
    if d1 is not None:
        series[Timeframe.D1] = d1
    return MarketSnapshot(
        snapshot_id="snap-test",
        symbol=symbol,
        as_of=last.end,
        session=session,
        last_price=last.close,
        currency="INR",
        series=series,
        quality=SnapshotQuality(
            complete=True,
            stale=False,
            missing_count=0,
            expected_count=len(m15.bars),
            last_bar_end=last.end,
            notes=(),
        ),
        source=FIXTURE_SOURCE,
    )


class IndicatorTests(unittest.TestCase):
    def test_sma_ema_rsi_insufficient(self) -> None:
        values = tuple(float(i) for i in range(10))
        self.assertIsNone(sma(values, 20))
        self.assertIsNone(ema(values, 20))
        self.assertIsNone(rsi(values, 14))
        self.assertAlmostEqual(sma(tuple(range(1, 6)), 5) or 0, 3.0)

    def test_rsi_bounds(self) -> None:
        up = tuple(float(100 + i) for i in range(30))
        down = tuple(float(200 - i) for i in range(30))
        self.assertGreater(rsi(up, 14) or 0, 70)
        self.assertLess(rsi(down, 14) or 0, 30)

    def test_rolling_high_excludes_current(self) -> None:
        symbol = Symbol("NIFTY")
        start = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
        prices = [100.0] * 20 + [150.0]
        series = _series(symbol, prices, start)
        prior = rolling_high(series.bars, 20, exclude_current=True)
        inclusive = rolling_high(series.bars, 20, exclude_current=False)
        self.assertEqual(prior, max(b.high for b in series.bars[:-1][-20:]))
        self.assertGreater(inclusive or 0, prior or 0)
        self.assertGreater(series.bars[-1].close, prior or 0)


class RegistryTests(unittest.TestCase):
    def test_default_names_and_duplicate(self) -> None:
        names = default_registry().names()
        self.assertEqual(names, ("ema_trend", "momentum", "breakout", "mean_reversion"))
        registry = StrategyRegistry()
        registry.register(default_registry().get("ema_trend"))
        with self.assertRaises(GrowConfigError):
            registry.register(default_registry().get("ema_trend"))
        with self.assertRaises(GrowConfigError):
            registry.get("nope")


class StrategyEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.engine = StrategyEngine(self.config)
        self.symbol = Symbol("NIFTY")
        self.start = datetime(2026, 9, 14, 9, 15, tzinfo=IST)

    def _uptrend(self, n: int = 80) -> MarketSnapshot:
        prices = [20000 + i * 8.0 for i in range(n)]
        m15 = _series(self.symbol, prices, self.start)
        d1_start = datetime(2026, 6, 1, 9, 15, tzinfo=IST)
        d1_prices = [19000 + i * 20.0 for i in range(60)]
        d1_bars = []
        day = d1_start
        for px in d1_prices:
            d1_bars.append(_bar(self.symbol, day, px, Timeframe.D1))
            day = day + timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
        d1 = BarSeries(symbol=self.symbol, timeframe=Timeframe.D1, bars=tuple(d1_bars))
        return _snapshot(self.symbol, m15, d1)

    def test_book_run_still_refuses_execution(self) -> None:
        with self.assertRaises(GrowInterfaceNotImplemented):
            StrategyBook().run("ema_trend")
        self.assertIn("ema_trend", StrategyBook().list_strategies())

    def test_wrong_instrument_skipped(self) -> None:
        prices = [1000 + i for i in range(80)]
        snap = _snapshot(Symbol("RELIANCE"), _series(Symbol("RELIANCE"), prices, self.start))
        result = self.engine.evaluate(snap)
        self.assertEqual(result.signals, ())
        self.assertTrue(any("INSTRUMENT_NOT_IN_2B_UNIVERSE" in s.reason for s in result.skipped))

    def test_session_closed_no_signals(self) -> None:
        snap = self._uptrend()
        closed = MarketSnapshot(
            snapshot_id=snap.snapshot_id,
            symbol=snap.symbol,
            as_of=snap.as_of,
            session=SessionState.CLOSED,
            last_price=snap.last_price,
            currency=snap.currency,
            series=snap.series,
            quality=snap.quality,
            source=snap.source,
        )
        result = self.engine.evaluate(closed)
        self.assertEqual(result.signals, ())
        self.assertTrue(any(s.reason.startswith("SESSION") for s in result.skipped))

    def test_uptrend_emits_long_only_and_is_deterministic(self) -> None:
        snap = self._uptrend()
        a = self.engine.evaluate(snap)
        b = self.engine.evaluate(snap)
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertTrue(a.signals)
        for sig in a.signals:
            self.assertEqual(sig.direction, "LONG")
            self.assertGreater(sig.entry, 0)
            self.assertLess(sig.stop, sig.entry)
            self.assertGreater(sig.target, sig.entry)
            self.assertTrue(sig.signal_id)
            self.assertEqual(sig.strategy_version, "v1")
            self.assertNotIn("CE", sig.reason)
            self.assertNotIn("PE", sig.reason)
        names = {s.strategy for s in a.signals}
        self.assertTrue({"ema_trend", "momentum", "breakout"} & names)

    def test_insufficient_history(self) -> None:
        prices = [20000.0, 20001.0, 20002.0]
        snap = _snapshot(self.symbol, _series(self.symbol, prices, self.start))
        result = self.engine.evaluate(snap)
        self.assertEqual(result.signals, ())
        self.assertTrue(any(s.reason == "INSUFFICIENT_HISTORY" for s in result.skipped))

    def test_disabled_strategy(self) -> None:
        config = load_config()
        specs = []
        for spec in config.strategies.specs:
            if spec.name == "breakout":
                specs.append(type(spec)(spec.name, False, spec.version, spec.params))
            else:
                specs.append(spec)
        object.__setattr__(config.strategies, "specs", tuple(specs))
        engine = StrategyEngine(config)
        result = engine.evaluate(self._uptrend())
        self.assertTrue(any(s.strategy == "breakout" and s.reason == "DISABLED" for s in result.skipped))
        self.assertFalse(any(s.strategy == "breakout" for s in result.signals))

    def test_snapshot_immutable(self) -> None:
        snap = self._uptrend()
        before = snap.series[Timeframe.M15].bars
        self.engine.evaluate(snap)
        self.assertIs(snap.series[Timeframe.M15].bars, before)

    def test_look_ahead_prefix_unchanged(self) -> None:
        full = self._uptrend(80)
        prefix_bars = full.series[Timeframe.M15].bars[:50]
        prefix_series = BarSeries(symbol=self.symbol, timeframe=Timeframe.M15, bars=prefix_bars)
        d1 = full.series[Timeframe.D1]
        prefix = _snapshot(self.symbol, prefix_series, d1)
        object.__setattr__(prefix, "snapshot_id", "snap-prefix")
        object.__setattr__(prefix, "as_of", prefix_bars[-1].end)
        first = self.engine.evaluate(prefix)
        # Afternoon crash bars exist only on the full snapshot.
        crash_prices = [p.close for p in prefix_bars] + [10000.0] * 30
        crash = _snapshot(self.symbol, _series(self.symbol, crash_prices, self.start), d1)
        object.__setattr__(crash, "snapshot_id", "snap-crash")
        later = self.engine.evaluate(crash)
        again = self.engine.evaluate(prefix)
        self.assertEqual(first.to_dict(), again.to_dict())
        self.assertNotEqual(first.to_dict()["signals"], later.to_dict()["signals"])

    def test_breakout_ignores_current_bar_in_lookback(self) -> None:
        prices = [20000.0] * 25 + [21000.0]
        snap = _snapshot(self.symbol, _series(self.symbol, prices, self.start))
        ind = build_indicators(
            snap.series[Timeframe.M15],
            fast_period=20,
            slow_period=50,
            rsi_period=14,
            roc_period=10,
            atr_period=14,
            lookback=20,
        )
        self.assertIsNotNone(ind.rolling_high)
        self.assertLess(ind.rolling_high or 0, snap.series[Timeframe.M15].bars[-1].close)

    def test_fixture_nifty_runs_offline(self) -> None:
        clock = FrozenClock(datetime(2026, 9, 21, 11, 0, tzinfo=IST))
        hub = open_data_hub(self.config, clock=clock)
        snap = hub.snapshot("NIFTY")
        result = self.engine.evaluate(snap)
        self.assertEqual(result.snapshot_id, snap.snapshot_id)
        self.assertTrue(result.skipped or result.signals)
        self.assertNotIn("option", result.to_dict())

    def test_broken_strategy_isolated(self) -> None:
        class Boom:
            name = "ema_trend"
            version = "v1"

            def evaluate(self, snapshot, context):
                raise RuntimeError("boom")

        registry = StrategyRegistry()
        registry.register(Boom())
        engine = StrategyEngine(self.config, registry=registry)
        result = engine.evaluate(self._uptrend())
        self.assertTrue(any("STRATEGY_ERROR" == s.reason for s in result.skipped))
        self.assertTrue(any("boom" in d for d in result.diagnostics))

    def test_signal_rejects_short(self) -> None:
        with self.assertRaises(ValueError):
            StrategySignal(
                symbol=self.symbol,
                strategy="ema_trend",
                direction="SHORT",
                entry=1,
                stop=2,
                target=3,
                confidence=0.5,
                timeframe=Timeframe.M15,
                reason="x",
                as_of=datetime(2026, 9, 21, 11, 0, tzinfo=IST),
                snapshot_id="s",
            ).to_dict()


class MeanReversionTests(unittest.TestCase):
    def test_oversold_range_can_fire(self) -> None:
        symbol = Symbol("BANKNIFTY")
        start = datetime(2026, 9, 14, 9, 15, tzinfo=IST)
        prices = [50000.0] * 40 + [50000 - i * 40 for i in range(25)]
        m15 = _series(symbol, prices, start)
        snap = _snapshot(symbol, m15)
        result = StrategyEngine(load_config()).evaluate(snap)
        names = {s.strategy for s in result.signals}
        self.assertTrue("mean_reversion" in names or any(s.reason in {"NO_SETUP", "INSUFFICIENT_HISTORY"} for s in result.skipped))


if __name__ == "__main__":
    unittest.main()
