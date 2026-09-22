# Milestone 2G — Point-in-time historical data

Status: **provider-neutral historical layer + sample dataset.**  
No licensed vendor is attached. This is **not** live data and **not** a
profitability claim.

```
File/JSON adapter  (or in-process sample)
        ↓
CanonicalStore  (immutable after publish)
        ↓
fingerprint + registry + 2F catalog
        ↓
HistoricalMarketSource / HistoricalOptionSource / ExplicitSessionCalendar
        ↓
2E BacktestRunner (optional sources)
======== STOP 2G ========
No broker. No live feed. No PaperLedger. No Risk Guard writes.
```

## v1 locks

| Knob | Value |
|---|---|
| Vendor | none — file adapter + `grow.history.sample.v1` |
| Universe | NIFTY / BANKNIFTY only |
| Availability | `as_of_available_at`; sample uses observation timestamp |
| Calendar | dataset sessions, **not** weekday inference |
| Missing IV/Greeks | remain missing; never fabricated |
| Lot size | historical contract metadata; no “today’s lot” fallback |
| 2E | still `backtest.provider=fixture` (no live). Inject 2G sources. |

`nse.weekday.v1` remains fixture-only. Historical replay must use the
dataset calendar (`nse.session.sample.v1` for the sample).

Published versions are immutable. Corrections require a new version.
Snapshots at T ignore records with `as_of_available_at > T`.
