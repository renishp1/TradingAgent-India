# Milestone 3A — Live market data → paper trading

Status: **mock stream drives the existing 2A–2D path into Risk Guard and PaperLedger.**  
No broker. No live order API. No silent fixture fallback.

```
LiveDataProvider (mock.v1)
        ↓
normalize  grow.stream.snapshot.v1
        ↓
freshness / sequence / timezone gates
        ↓
2A MarketSnapshot + 2C OptionChainSnapshot
        ↓
2B StrategyEngine
        ↓
2C IndexOptionsEngine  (options.provider=paper_stream on a loop copy)
        ↓
2D ResearchOrchestrator / CEO
        ↓
Risk Guard
        ↓
PaperLedger
======== STOP 3A ========
No broker. No live trading. No 2A allow_live_feed flip.
```

## v1 locks

| Knob | Value |
|---|---|
| Stream provider | `mock` only (`grow.stream.mock.v1`) |
| `live_data.paper_mode` | true |
| `live_data.live_trading` | false |
| `data.allow_live_feed` | remains false |
| `options.allow_live_chain` | remains false |
| 2C provider used by the loop | `paper_stream` (not `live`, not fixture fallback) |
| Source id | `grow.stream.mock.v1` — the substring `live` stays forbidden in 2C |
| Universe | 2I `IndexUniverseRegistry` overlay; not hard-coded to NIFTY/BANKNIFTY |
| Same-day expiry | still forbidden |
| BULLISH / BEARISH | BUY CE / BUY PE only |
| Fill | ask + slippage; missing ask → no fill |
| Lot size | provider-supplied; missing → `MISSING_LOT_SIZE` |
| IV / Greeks | never fabricated |

`paper_stream` is a paper-trading chain mode. It is not a broker venue and it
cannot be selected unless `live_data.enabled=true`.

Milestone 3C is provider-neutral live market data. Approved providers include
`mock`, `kite_market` (current real Zerodha/Kite quote path), and the
historical/legacy `truedata` adapter (`grow.stream.truedata.v1` — see
[`docs/truedata.md`](truedata.md)). Mock remains the CI default.

A fixture or historical payload presented to the mock adapter fails closed
with `FIXTURE_FALLBACK_FORBIDDEN`.

## Freshness

Snapshots use IST-aware `event_time` and `received_time`. Fail-closed:

| Condition | Result |
|---|---|
| Naive timestamp | `NAIVE_TIMESTAMP` |
| `event_time > now` | `FUTURE_SNAPSHOT` — never treated as fresh |
| `received_time > now` | `FUTURE_RECEIVED_TIME` |
| `received_time < event_time` | `TIMESTAMP_INVERTED` |
| age ≤ `max_staleness_seconds` | `freshness_ok=true` |
| age > `max_staleness_seconds` | `STALE` / NO_TRADE; health `STALE` |

A future timestamp is rejected even if `age <= max_staleness_seconds`
(negative age is not freshness).

## Timing (Clock-gated, no internal scheduler)

`run_once()` is pull-based. An external runner owns sleep/scheduling.
The loop only refuses a cycle using the injected `Clock`:

| Knob | Behaviour |
|---|---|
| `session_timeout_seconds` | Session age ≥ timeout → `stop()`, health `STOPPED`, `SESSION_TIMEOUT` / NO_TRADE. The session cannot continue. |
| `snapshot_interval_seconds` | Measured from **completion** of a cycle that processed a valid snapshot. A later `run_once()` before the interval has elapsed returns `SNAPSHOT_INTERVAL` / NO_TRADE without polling. Provider failures and invalid/future/out-of-order/duplicate snapshots do not start the interval. `0` disables the gate. |

These gates are deterministic under `FrozenClock.advance()`.

Milestone 3B (paper MTM + deterministic exits) lives in [`docs/positions.md`](positions.md).

## Session health

`DISCONNECTED` → `CONNECTING` → `READY` → `RUNNING`.  
`STALE` / `DEGRADED` / `STOPPED` are NO_TRADE.

`PIT`/historical qualification rules in 2G–2J are unchanged.
