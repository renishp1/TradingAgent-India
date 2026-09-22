# Milestone 3C — Real Indian live market-data adapter (TrueData)

Status: **TrueData adapter feeds the existing 3A/3B paper loop.**  
No broker. No live orders. No silent fixture fallback.

```
TrueData (licensed operator subscription)
        ↓
TrueDataAdapter  grow.stream.truedata.v1
        ↓
canonical grow.stream.snapshot.v1
        ↓
existing 3A freshness / order / timezone gates
        ↓
2A → 2B → 2C → 2D → Risk Guard → 3B paper OPEN/CLOSE
```

## Locks

| Knob | Value |
|---|---|
| Config provider | `live_data.provider=truedata` |
| Stream identity | `grow.stream.truedata.v1` (the substring `live` is still forbidden in 2C) |
| 2C mode used by the loop | `paper_stream` |
| Universe | provider catalog + 2I policy overlay; not hard-coded to NIFTY/BANKNIFTY |
| Same-day expiry | forbidden |
| Lot size | catalog / contract master; missing → `MISSING_LOT_SIZE` |
| IV / Greeks | copied only when the vendor supplies them |
| Credentials | `TRUEDATA_USERNAME` / `TRUEDATA_PASSWORD` env only |
| CI default | `provider=mock`, `enabled=false`, `mode=replay` |
| Reconnect | `bounded_backoff` (TrueData) or `fail_closed` |
| Broker / live trading | forbidden |

Captured vendor messages under `tests/fixtures/truedata/` are adapter-contract fixtures.
They are **not** approved 2E historical datasets.

## Subscription

The adapter subscribes to index spots plus a configurable `subscription_strike_window`
around ATM (default 4). 2C still selects ATM ±2. Vendor `max_symbols` is enforced
before subscribe. On reconnect the full desired set is resubscribed.

## Manual smoke

See `scripts/run_truedata_smoke.py`. Requires an explicit licensed subscription.
Do not set any broker token or `live_trading` flag.
