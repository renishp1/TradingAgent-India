"""Strategy engine orchestration. Research only — no execution, no options."""

from __future__ import annotations

from grow.config import GrowConfig, StrategySpec
from grow.data.schema import Bar, BarSeries, MarketSnapshot, Timeframe
from grow.errors import GrowConfigError
from grow.strategies.indicators import (
    atr,
    candle_body,
    candle_range,
    closes,
    ema,
    ema_slope,
    lower_wick,
    previous_high,
    previous_low,
    roc,
    rolling_high,
    rolling_low,
    rolling_std,
    rsi,
    sma,
    upper_wick,
)
from grow.strategies.models import (
    IndicatorSnapshot,
    StrategyContext,
    StrategyResult,
    StrategySkip,
)
from grow.strategies.regime import classify
from grow.strategies.registry import StrategyRegistry, default_registry
from grow.strategies.validate import validate_signal
from grow.types import SessionState


def build_indicators(
    series: BarSeries,
    *,
    fast_period: int,
    slow_period: int,
    rsi_period: int,
    roc_period: int,
    atr_period: int,
    lookback: int,
) -> IndicatorSnapshot:
    bars = series.bars
    if not bars:
        raise GrowConfigError("cannot compute indicators on an empty series")
    last = bars[-1]
    values = closes(bars)
    return IndicatorSnapshot(
        timeframe=series.timeframe,
        close=last.close,
        sma_fast=sma(values, fast_period),
        ema_fast=ema(values, fast_period),
        ema_slow=ema(values, slow_period),
        ema_fast_slope=ema_slope(values, fast_period),
        rsi=rsi(values, rsi_period),
        roc=roc(values, roc_period),
        atr=atr(bars, atr_period),
        rolling_std=rolling_std(values, fast_period),
        prev_high=previous_high(bars),
        prev_low=previous_low(bars),
        rolling_high=rolling_high(bars, lookback, exclude_current=True),
        rolling_low=rolling_low(bars, lookback, exclude_current=True),
        range=candle_range(last),
        body=candle_body(last),
        upper_wick=upper_wick(last),
        lower_wick=lower_wick(last),
        bar_count=len(bars),
    )


class StrategyEngine:
    def __init__(self, config: GrowConfig, registry: StrategyRegistry | None = None) -> None:
        self.config = config
        self.registry = registry or default_registry()

    def evaluate(self, snapshot: MarketSnapshot) -> StrategyResult:
        cfg = self.config.strategies
        diagnostics: list[str] = []
        skipped: list[StrategySkip] = []
        evaluated: list[str] = []
        signals = []

        primary = Timeframe(cfg.primary_timeframe)
        empty_ind = _empty_indicators(primary)
        if snapshot.symbol.ticker not in cfg.universe:
            skipped.append(StrategySkip("*", f"INSTRUMENT_NOT_IN_2B_UNIVERSE:{snapshot.symbol.ticker}"))
            return _result(snapshot, skipped, evaluated, signals, diagnostics, empty_ind)
        if snapshot.session is not SessionState.OPEN:
            skipped.append(StrategySkip("*", f"SESSION:{snapshot.session.value}"))
            return _result(snapshot, skipped, evaluated, signals, diagnostics, empty_ind)
        if primary not in snapshot.series:
            skipped.append(StrategySkip("*", f"MISSING_TIMEFRAME:{primary.value}"))
            return _result(snapshot, skipped, evaluated, signals, diagnostics, empty_ind)

        series = snapshot.series[primary]
        periods = _periods(cfg.specs)
        indicators = build_indicators(series, **periods)
        d1_bars: tuple[Bar, ...] = snapshot.series[Timeframe.D1].bars if Timeframe.D1 in snapshot.series else ()
        regime = classify(
            m15=indicators,
            d1_bars=d1_bars,
            fast_period=periods["fast_period"],
            slow_period=periods["slow_period"],
            atr_period=periods["atr_period"],
        )

        seen: set[tuple[str, ...]] = set()
        for spec in cfg.specs:
            if not spec.enabled:
                skipped.append(StrategySkip(spec.name, "DISABLED"))
                continue
            try:
                strategy = self.registry.get(spec.name)
            except GrowConfigError as exc:
                diagnostics.append(str(exc))
                skipped.append(StrategySkip(spec.name, "UNKNOWN_STRATEGY"))
                continue
            evaluated.append(spec.name)
            params = {"version": spec.version, **dict(spec.params)}
            context = StrategyContext(
                as_of=snapshot.as_of,
                timeframe=primary,
                regime=regime,
                indicators=indicators,
                snapshot_id=snapshot.snapshot_id,
                session=snapshot.session,
                params=params,
            )
            try:
                signal = strategy.evaluate(snapshot, context)
            except Exception as exc:  # noqa: BLE001 — isolate a broken strategy
                diagnostics.append(f"{spec.name}: {type(exc).__name__}: {exc}")
                skipped.append(StrategySkip(spec.name, "STRATEGY_ERROR"))
                continue
            if signal is None:
                reason = "INSUFFICIENT_HISTORY" if _needs_history(spec.name, indicators) else "NO_SETUP"
                skipped.append(StrategySkip(spec.name, reason))
                continue
            try:
                validate_signal(signal, snapshot_id=snapshot.snapshot_id, allowed_symbols=cfg.universe)
            except GrowConfigError as exc:
                diagnostics.append(f"{spec.name} invalid: {exc}")
                skipped.append(StrategySkip(spec.name, "INVALID_SIGNAL"))
                continue
            identity = (
                snapshot.snapshot_id,
                signal.strategy,
                signal.symbol.ticker,
                signal.timeframe.value,
                signal.direction,
                signal.strategy_version,
            )
            if identity in seen:
                skipped.append(StrategySkip(spec.name, "DUPLICATE"))
                continue
            seen.add(identity)
            signals.append(signal)

        return _result(snapshot, skipped, evaluated, signals, diagnostics, indicators, regime)

    def list_strategies(self) -> tuple[str, ...]:
        return self.registry.names()


def _needs_history(name: str, indicators: IndicatorSnapshot) -> bool:
    required = {
        "ema_trend": ("ema_fast", "ema_slow", "ema_fast_slope", "atr"),
        "momentum": ("rsi", "roc", "ema_fast", "atr"),
        "breakout": ("rolling_high", "rolling_low", "atr"),
        "mean_reversion": ("rsi", "ema_fast", "atr"),
    }
    fields = required.get(name, ())
    return not indicators.ready(*fields) if fields else False


def _periods(specs: tuple[StrategySpec, ...]) -> dict[str, int]:
    params = {}
    for spec in specs:
        params.update(spec.params)
    return {
        "fast_period": int(params.get("fast_period", 20)),
        "slow_period": int(params.get("slow_period", 50)),
        "rsi_period": int(params.get("rsi_period", 14)),
        "roc_period": int(params.get("roc_period", 10)),
        "atr_period": int(params.get("atr_period", 14)),
        "lookback": int(params.get("lookback", 20)),
    }


def _empty_indicators(timeframe: Timeframe) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        timeframe=timeframe,
        close=0.0,
        sma_fast=None,
        ema_fast=None,
        ema_slow=None,
        ema_fast_slope=None,
        rsi=None,
        roc=None,
        atr=None,
        rolling_std=None,
        prev_high=None,
        prev_low=None,
        rolling_high=None,
        rolling_low=None,
        range=None,
        body=None,
        upper_wick=None,
        lower_wick=None,
        bar_count=0,
    )


def _result(snapshot, skipped, evaluated, signals, diagnostics, indicators, regime=None) -> StrategyResult:
    from grow.strategies.models import MarketRegime, RegimeSnapshot

    if regime is None:
        regime = RegimeSnapshot(
            label=MarketRegime.UNKNOWN,
            d1_trend="unknown",
            m15_trend="unknown",
            volatility="unknown",
            reason="not evaluated",
        )
    return StrategyResult(
        snapshot_id=snapshot.snapshot_id,
        as_of=snapshot.as_of,
        symbol=snapshot.symbol,
        regime=regime,
        signals=tuple(signals),
        evaluated=tuple(evaluated),
        skipped=tuple(skipped),
        diagnostics=tuple(diagnostics),
        indicators=indicators,
    )
