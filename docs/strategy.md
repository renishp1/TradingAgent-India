# Milestone 2B — quantitative strategy engine

Status: **implemented. Signals only.**  
2B stops at `StrategySignal`. It does not trade, select options, or call an LLM.

```
2A MarketSnapshot
      ↓
Indicator engine (SMA/EMA/RSI/ATR/ROC/structure)
      ↓
Regime engine (deterministic)
      ↓
Strategy registry (ema_trend, momentum, breakout, mean_reversion)
      ↓
Validate + dedupe
      ↓
StrategyResult { signals[], regime, diagnostics }
      ↓
======== STOP 2B ========
2C index options  →  2D CEO  →  Risk Guard  →  paper
```

## Responsibility

`StrategyEngine.evaluate(snapshot)` is a pure research function over a
validated `MarketSnapshot`. Same snapshot + same config + same strategy
version always yields the same `StrategyResult`.

It must not:

- trade options or name a CE/PE
- read an option chain, IV, Greeks, OI, premium
- connect a broker or place an order
- mutate the snapshot or the ledger
- use candles with `end > as_of`
- call an LLM

`StrategyBook.run` still raises. Execution stays in Risk Guard → paper.

## Universe and session

2B instruments: `NIFTY`, `BANKNIFTY` (config `grow.strategies.universe`).
Primary timeframe: `M15` (D1 is regime context, M5 is available but not
the default entry series).

LONG research signals are emitted only while `session == OPEN`.
`StrategySignal.direction` is `LONG` only. No SHORT, SELL, or OPTION_SELL.

A LONG underlying signal is **not** a BUY CE. 2C will map bullish/bearish
research onto option *buying*. 2B does not perform that mapping.

## Indicators

Computed once per snapshot, then reused:

| Name | Rule |
|---|---|
| SMA / EMA | `None` if `len < period` — no silent shorter window |
| EMA slope | last EMA − previous EMA |
| RSI(14) | Wilder. `None` if `len < 15` |
| ROC | percent change vs `period` bars ago |
| ATR | mean true range |
| rolling high/low | **previous completed bars only** (`exclude_current=True`) |

Insufficient history → strategy skip `INSUFFICIENT_HISTORY`, not a fake score.

## Regime

Deterministic from D1 + M15 EMA alignment and ATR% of price:

`BULL_TREND | BEAR_TREND | RANGE | HIGH_VOLATILITY | LOW_VOLATILITY | UNKNOWN`

Informational. Strategies use it as explicit eligibility, not a hidden veto.

## Strategies (v1)

| Name | Idea | Eligibility |
|---|---|---|
| `ema_trend:v1` | EMA fast > slow, positive slope, price above fast | skip BEAR/RANGE |
| `momentum:v1` | RSI > threshold, ROC > 0, price > EMA | skip BEAR |
| `breakout:v1` | close > prior N-bar high (current bar excluded) | skip RANGE/BEAR |
| `mean_reversion:v1` | RSI oversold and price below EMA | RANGE / LOW_VOL / UNKNOWN |

Parameters live in `configs/grow.default.yaml` under `grow.strategies.*`.
Changing logic requires `v1 → v2`. Do not silently retune v1.

## Signal identity

`signal_id` is SHA-256 of
`symbol, strategy, timeframe, as_of, snapshot_id, strategy_version, direction`
(first 16 hex). Duplicates of that identity in one evaluation are dropped.

Confidence is a bounded count of confirming factors, documented in code,
not an LLM score.

Entry / stop / target are **underlying** prices. Risk and reward must be
`> 0` or the signal is rejected (`INVALID_SIGNAL`).

## Engine result

`StrategyResult` keeps every strategy's outcome: emitted LONG signals,
skipped reasons (`DISABLED`, `NO_SETUP`, `INSUFFICIENT_HISTORY`, …),
exceptions isolated per strategy (`STRATEGY_ERROR` + diagnostic string).
A broken strategy does not drop the others.

Multiple strategies may disagree. 2B does **not** pick a winner.

## Tests

Look-ahead: a 10:00/prefix snapshot must not change when later bars exist
on a different snapshot. Rolling high excludes the current bar. Snapshot
bar tuples keep identity after `evaluate`. Fixture NIFTY/BANKNIFTY runs
offline.
