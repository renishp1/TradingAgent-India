# Milestone 2J — Real historical provider integration & qualification

Status: **one explicit recorded NSE F&O-shaped adapter + qualification flow.**  
No licensed vendor HTTP client is attached. This is **not** a live feed,
**not** a broker, and **not** a profitability claim.

```
Explicit provider_id = recorded.nse_fo.v1
        ↓
RecordedHistoricalProvider.acquire  (recorded payload, retries, pagination)
        ↓
RawArtifact  (immutable fingerprint, secrets stripped)
        ↓
Normalizer  nse.fo.recorded.v1 → CanonicalStore
        ↓
publish + PIT + ProviderEvaluationRunner
        ↓
QUALIFIED / APPROVED_FOR_2E  (or REJECTED)
        ↓
2E BacktestRunner / 2F catalog freeze
======== STOP 2J ========
No broker. No live feed. No PaperLedger. No Risk Guard writes.
No silent fallback to fixture data.
```

## v1 locks

| Knob | Value |
|---|---|
| Approved provider | `recorded.nse_fo.v1` only — selected by explicit `AcquireScope.provider_id` |
| Vendor schema | `nse.fo.recorded.v1` (distinct from 2G/2H fixture samples) |
| Adapter version | `history.integrate.v1` |
| Network | none — recorded payloads only; no urllib / vendor SDK |
| Secrets | `GROW_HISTORICAL_*` environment keys; never committed; stripped from artifacts |
| Fixture fallback | `FIXTURE_FALLBACK_FORBIDDEN` |
| Live | `PROVIDER_NOT_APPROVED` / `LIVE_CHAIN_FORBIDDEN` |
| Universe | discovered from contract master; registry is the approval overlay |
| Identity | canonical `{U}-{expiry}-{strike}-{CE\|PE}`; `tradingsymbol` stays provider id |
| Lot size | historical contract metadata; drives 2E P&L and costs |
| IV / Greeks | ingested only when present; never fabricated |
| Timestamps | timezone-aware at the canonical boundary; naive ISO fails |
| 2E consume | `QUALIFIED` or `APPROVED_FOR_2E` only |
| 2E `backtest.provider` | remains `fixture` (execution simulation). `options.provider=historical` |
| 2F | freeze binds dataset fingerprint + version; cannot override rejection |

NIFTY and BANKNIFTY stay weekly-preferred defaults. MIDCPNIFTY is discovered
from recorded contracts and approved as `MONTHLY_ONLY` by the existing
`IndexUniverseRegistry`. RELIANCE / stock options / futures are rejected.

## Lifecycle

`RAW_ACQUIRED` → `NORMALIZED` → `PIT_VALIDATED` → `QUALIFICATION_REVIEW` →
`QUALIFIED` / `REJECTED`.

A dataset fingerprint is immutable after publish. Incomplete coverage,
identity ambiguity, naive timestamps, unverifiable provenance, and PIT
failures fail closed. `QUALIFIED_WITH_WARNINGS` is not admitted to 2E
replay (`DATASET_NOT_QUALIFIED`).

## 2E / 2F

`open_qualified_runner` injects `HistoricalMarketSource` +
`HistoricalOptionSource` from the qualified store. The run manifest records
dataset id, version, fingerprint, and provider. Fixture mode is unchanged.

`catalog_row` / `bind_director_catalog` expose the qualified fingerprint to
the Research Director. Changing dataset version after freeze yields a new
plan fingerprint. The Director cannot override qualification rejection.

## What 2J does not do

- It does not attach NSE / TrueData / Global Datafeeds HTTP.
- It does not auto-select a vendor by convenience.
- It does not scrape HTML.
- It does not add a broker, live chain, or paper-ledger writes.
- It does not let an LLM override qualification, PIT, or Risk Guard.
