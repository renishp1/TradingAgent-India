# Milestone 2A — market-data architecture

Status: **design implemented, no live feed attached.**  
Review this commit before connecting any vendor.

Grow remains paper-only. Broker execution stays disconnected.
`LIVE_TRADING_COMPILED = False`. Option chains stay unimplemented.

## Why 2A exists

Milestone 1 gave Grow a control plane:

```
Market Research (stub quote)
        ↓
      CEO
        ↓
   Risk Guard → RiskStamp
        ↓
   Paper Ledger
```

That plane is **accepted** and is not being rewired here.

2A adds a **data plane** next to it, not a shortcut around it:

```
                DATA LAYER
                    │
          ┌─────────┴─────────┐
          │                   │
       OHLCV              Index levels
          │                   │
          └─────────┬─────────┘
                    ↓
             Data Normalizer
                    ↓
             MarketSnapshot          ← quant only
                    ↓
             ResearchView            ← LLM-safe summary, no candles
                    ↓
          (2B) StrategySignal        ← type frozen, book not implemented
                    ↓
               CEO / Risk / Paper    ← unchanged in 2A
```

The paper cycle does **not** consume `DataHub` yet. Connecting snapshots to
the CEO is 2B, after this architecture is reviewed.

## What is in this commit

Cash market only.

| Item | 2A |
|---|---|
| NIFTY 50 seed universe | yes, freeze `2026-09-21`, **not** the official circular |
| NIFTY indices | `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `NIFTYIT` |
| Selected NSE stocks | the existing 10-name paper book |
| Daily OHLCV | fixture, 20 sessions |
| 15-minute OHLCV | fixture, 25 bars / full cash session |
| 5-minute OHLCV | fixture, 75 bars / full cash session |
| Volume | synthetic, always ≥ 0 |
| Source metadata | `grow.data.fixture.v1`, `is_live=false` |
| Freshness | `stale_after_seconds` (default 900) |
| Missing-candle detection | expected vs actual bar starts |
| Exchange session calendar | existing IST cash calendar, still a seed holiday list |
| Corporate actions | identity adjuster; real files refuse |
| Licensed / live feed | **refuse-closed** |
| HTML scrape / vendor SDK | forbidden by tests |
| Options chain | still `GrowInterfaceNotImplemented` |
| Broker | still impossible |

## Legal source

The only constructed source is `FixtureSource`, wired through
`open_data_hub()`. `DataHub` takes a `MarketDataSource` and does not import
the fixture.

Intraday prices are a hash of `(ticker, bar.start)`. They must not use a
session-day EOD seed. Forming D1 at 11:00 is the M5 prefix 09:15→11:00;
complete D1 at 15:30 is the full-session M5 aggregate.

`data.provider` must be `fixture`. `data.allow_live_feed` must be false.
`LicensedFeed()` raises. Setting `GROW_DATA_PROVIDER` to anything else
refuses to boot.

A later adapter, after review, must:

1. Name the vendor and the licence.
2. Emit `Bar` / `MarketSnapshot` only (no vendor JSON past the normalizer).
3. Carry `SourceMeta.is_live` and a licence string.
4. Adjust for corporate actions **before** quant sees the series.
5. Still not talk to the ledger or any broker.

Do not scrape NSE/BSE HTML. Do not add `yfinance`, `nsepython`, or broker
quote sockets in this review.

## Session math

NSE cash `09:15`–`15:30` IST.

| Timeframe | Full session | At 11:00 IST |
|---|---|---|
| M15 | 25 (last start 15:15) | 7 complete |
| M5 | 75 (last start 15:25) | 21 complete |
| D1 | 1 per session (forming 09:15→as_of until 15:30) | forming 09:15→11:00 from complete M5 |

A bar is complete when `start + duration <= as_of`.

## Quant vs LLM

Raw candles are not a prompt.

- `MarketSnapshot.series` → quantitative code (2B).
- `ResearchView` → LLM / TradingAgents graph. Contains last price, session,
  quality flags, bar **counts**, source name. Contains **no** OHLCV arrays.
- `assert_research_payload` fails closed if `bars` / `series` / OHLC keys leak.

`StrategySignal` is frozen as the 2B contract:

```
symbol, strategy, direction=LONG, entry, stop, target,
confidence, timeframe, reason, as_of, snapshot_id
```

`StrategyBook.run` still raises. Cash signals are long-only.

## DecisionRecord (backlog)

`grow.learning.record.DecisionRecord` is the audit object for later. It is
**not** part of `RiskStamp`. Thesis, model, prompt hash, and signals stay
out of the execution HMAC.

## What this commit does not do

- Does not attach a licensed feed.
- Does not change Risk Guard, HMAC, or the paper lock.
- Does not change CEO 2%/3% probe placeholders.
- Does not compute MTM equity (still `cost_notional`).
- Does not implement 2B strategies.
- Does not implement options.
- Does not implement brokers.

## Review checklist

- [ ] Fixture-only boot is the only legal 2A path
- [ ] NIFTY 50 seed is documented as unofficial
- [ ] Holiday calendar is still a seed
- [ ] No scrape / vendor client in `grow/data`
- [ ] ResearchView cannot carry candles
- [ ] LicensedFeed refuses
- [ ] Paper lock unchanged
- [ ] Named vendor + licence **before** any real socket
