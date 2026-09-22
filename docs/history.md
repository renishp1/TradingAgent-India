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

2H evaluation: [`docs/provider.md`](provider.md).
2J recorded provider: [`docs/integrate.md`](integrate.md).
```

## v1 locks

| Knob | Value |
|---|---|
| Vendor | none — file adapter + `grow.history.sample.v1` |
| Sample usage | `FRAMEWORK_TEST_ONLY` / `SYNTHETIC` — not historical research |
| Warnings | `APPROVED_WITH_WARNINGS` requires explicit IDs in `accepted_dataset_warnings` |
| Slot mapping | v1 fixture `EXACT` / 0s; `NEAREST_WITHIN_TOLERANCE` reserved, not applied |
| Calendar | dataset `HistoricalSession`; missing weekday → `CALENDAR_MISSING` / `DATA_UNAVAILABLE` |
| Option cadence | `snapshot_cadence` (sample: 11:00 and 15:15 IST); one quote/day is not 100% |
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

## Dataset warnings

Material warnings are dataset-version IDs, not a blanket flag:

- `MISSING_IV`
- `BID_ASK_GAPS`
- `OPTION_SNAPSHOT_GAPS`
- `MISSING_OI`
- `MISSING_VOLUME`
- `MISSING_SESSIONS`

A research plan must list **exactly** those IDs via
`FixtureDirector.plan(accepted_dataset_warnings=...)`. Unknown IDs fail
`UNKNOWN_WARNING_ID`. Partial acceptance fails `WARNINGS_NOT_ACKNOWLEDGED`.
Accepted IDs are part of the frozen plan fingerprint.

## Vendor slot mapping (prepared, not applied)

v1 fixture/sample coverage matches `snapshot_cadence` **exactly**.
`mapping_policy` / `slot_tolerance_seconds` are stored so a licensed vendor
adapter can later use `NEAREST_WITHIN_TOLERANCE` without pretending 11:00:00
exists on every feed. v1 does **not** implement nearest matching.
