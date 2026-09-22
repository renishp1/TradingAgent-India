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
The map is **connection-scoped**:

1. `disconnect` / reconnect start **clears** `_symbol_ids` and sets `mapping_ready=False`.
2. Fresh authenticate, then resubscribe the desired set.
3. `symbolsadded` rebuilds `symbol_id → provider_symbol` for this connection only.
4. `mapping_ready=True` only after that ack covers the current subscription.
5. A tick with `symbol_id` while `mapping_ready=False` fails closed: `SYMBOL_MAP_NOT_READY`.
6. An id absent from the **current** map fails closed: `UNKNOWN_SYMBOL_ID`.
7. Catalog `provider_symbol_id` is metadata only. It is never reused as a live tick map.
8. `provider_symbol` stays the vendor identity. Canonical contract id
   (`NIFTY-2026-09-22-25000-CE`) is never used as the vendor symbol.

Previous-connection ids (e.g. `101` after a reconnect that mapped `201`) are rejected.

## Expiry class

TrueData symbol lists usually omit `expiry_class`. Grow never infers WEEKLY
from option-ness.

Milestone 3C.1 adds `grow.live_data.expiry_class.ExpiryClassifier`
(`expiry.class.nse.v1`) on top of the 2026 cash session calendar
(`nse.session.cash.2026.v1`).

Precedence:

1. Explicit provider `WEEKLY` / `MONTHLY` that **agrees** with the calendar.
2. Otherwise the versioned weekday calendar (weekly weekday, last monthly
   weekday of the month, holiday-adjusted to the previous session).
3. Provider vs calendar disagreement → `UNKNOWN` + `CLASSIFICATION_CONFLICT`.
4. No schedule for the underlying → `UNKNOWN` + `CLASSIFIER_NOT_READY`.
5. Anything else → `UNKNOWN` + `UNKNOWN_EXPIRY_CLASS`.

| Result | Eligible for 2I selection / live subscription |
|---|---|
| `WEEKLY` | yes |
| `MONTHLY` | yes |
| `UNKNOWN` | **no** |

The classifier is keyed by provider symbol + expiry + policy/calendar
versions. A reconnect recomputes when those versions change; it does not
reuse a stale class. 2I profiles (`WEEKLY_PREFERRED`, `MONTHLY_ONLY`,
`WEEKLY_THEN_MONTHLY`) are unchanged — they only see classified contracts.

Default weekday schedules are a versioned policy overlay (not a hard-coded
NIFTY/BANKNIFTY universe). An index without a schedule stays UNKNOWN even
if the 2I overlay would otherwise allow it.

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
