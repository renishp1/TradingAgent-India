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

Timeframes:

| Role | Timeframe |
|---|---|
| Primary (signals) | **M15** (locked) |
| Context | D1 regime, M5 available but not the signal series |

`primary_timeframe` cannot be set to M5 or D1.

Research signals are emitted only while `session == OPEN`.

`StrategySignal.direction` is **research** language:

| Research | Later 2C mapping (not in 2B) |
|---|---|
| `BULLISH` | candidate BUY CE |
| `BEARISH` | candidate BUY PE |

Not `BUY`, `SELL`, `LONG`, `SHORT`, or `OPTION_SELL`. 2B does not execute
and does not select an option contract.

A `BEARISH` signal is **not** a short sale of the index. The cash book
remains long-only. 2C will buy puts.

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

| Name | BULLISH | BEARISH |
|---|---|---|
| `ema_trend:v1` | EMA fast > slow, +slope, price above fast | EMA fast < slow, −slope, price below fast |
| `momentum:v1` | RSI > threshold, ROC > 0, price > EMA | RSI < 100−threshold, ROC < 0, price < EMA |
| `breakout:v1` | close > prior N-bar high (current excluded) | close < prior N-bar low (current excluded) |
| `mean_reversion:v1` | RSI oversold, price below EMA | RSI overbought, price above EMA |

Trend/momentum/breakout skip the opposite trend regime. Mean reversion
is RANGE / LOW_VOL / UNKNOWN.

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

`StrategyResult` keeps every strategy's outcome: emitted BULLISH/BEARISH
research signals, skipped reasons (`DISABLED`, `NO_SETUP`,
`INSUFFICIENT_HISTORY`, …), exceptions isolated per strategy
(`STRATEGY_ERROR` + diagnostic string). A broken strategy does not drop
the others.

Multiple strategies may disagree. 2B does **not** pick a winner.

## Tests

Look-ahead: a 10:00/prefix snapshot must not change when later bars exist
on a different snapshot. Rolling high excludes the current bar. Snapshot
bar tuples keep identity after `evaluate`. Fixture NIFTY/BANKNIFTY runs
offline.
