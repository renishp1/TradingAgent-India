# Milestone 3C — Real Indian live market-data adapter (TrueData)

Status: **TrueData adapter feeds the existing 3A/3B paper loop.**  
No broker. No live orders. No silent fixture fallback.

```
TrueData WebSocket
        ↓
protocol decoder  (auth / heartbeat / subscribe / trade / error / disconnect)
        ↓
catalog bootstrap  (vendor symbol lists, not a hard-coded universe)
        ↓
2I IndexUniverseRegistry overlay
        ↓
ATM subscription + symbol-id map
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

## Protocol

`grow.live_data.protocol.decode_truedata_message` classifies vendor frames
**before** tick ingest:

| Frame | Kind | Trading effect |
|---|---|---|
| `{success, message: "TrueData Real Time Data Service"}` | `auth` | none |
| `{HeartBeat: ...}` | `heartbeat` | connection health only |
| `{symbolsadded: [[symbol, id], ...]}` | `subscribe` | store symbol-id map |
| `{trade: [id, ts, ltp, ...]}` or L1 CSV | `tick` | market data |
| `{error: ...}` | `error` | `PROVIDER_ERROR` / DEGRADED |
| `{message: "user disconnected"}` | `disconnect` | `FEED_DISCONNECTED` |
| malformed JSON | — | `MALFORMED_MESSAGE` |

The paper loop treats control kinds (`auth`, `heartbeat`, `subscribe`, `catalog`)
as `FEED_*` / `NO_TRADE`. They do not create a snapshot, do not advance the
market-data sequence, and do not update option prices.

Heartbeats and last market activity are tracked separately. If neither arrives
within `max_staleness_seconds`, the adapter goes `DEGRADED` with
`STALE_REQUIRED_QUOTE` and no new trades are opened.

## Symbol-ID mapping

Live ticks often carry a numeric Symbol ID, not the tradingsymbol.

1. `addsymbol` / `symbolsadded` responses store `symbol_id → provider_symbol`.
2. Catalog rows may also contribute `provider_symbol_id`.
3. A tick with an unknown id fails closed: `UNKNOWN_SYMBOL_ID`.
4. `provider_symbol` stays the vendor identity. Canonical contract id
   (`NIFTY-2026-09-22-25000-CE`) is never used as the vendor symbol.

## Catalog discovery

Real mode bootstraps instruments from TrueData symbol lists
(`NSE_SPOT_INDEX` + `NSE_INDICES_OPTIONS`), then applies the 2I policy overlay
and nearest eligible expiry. FINNIFTY (and any later index) is discoverable
only when the overlay allows it.

Missing or failed catalog → `METADATA_UNAVAILABLE` + `DEGRADED`. The adapter
does **not** fall back to a hard-coded NIFTY/BANKNIFTY universe or to fixture
data.

Without a live spot, only index symbols are subscribed. The first live index
tick recalculates the ATM window and subscribes to that expiry's CE/PE set.

## Subscription

The adapter subscribes to index spots plus a configurable `subscription_strike_window`
around ATM (default 4). 2C still selects ATM ±2. Vendor `max_symbols` is enforced
before subscribe. On reconnect the full desired set is resubscribed after a
fresh authenticate.

## Manual smoke

See `scripts/run_truedata_smoke.py`. Requires an explicit licensed subscription.
The script authenticates, fetches the vendor catalog itself, subscribes, waits
for the first live snapshot (skipping heartbeat/control frames), then prints
paper-only diagnostics. Do not inject a catalog file. Do not set any broker
token or `live_trading` flag.
