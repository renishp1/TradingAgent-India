# Milestone 2H — Provider evaluation & nearest eligible expiry

Status: **evaluation harness + synthetic provider-native sample.**  
No licensed vendor is attached. This is **not** live data and **not** a
profitability claim.

```
Provider-native export  (file / sample)
        ↓
Provider adapter (EXACT mapping in v1)
        ↓
2G CanonicalStore
        ↓
Historical expiry universe at as_of
        ↓
2C choose_expiry (nearest eligible WEEKLY, same-day forbidden)
        ↓
ATM ±2 / liquidity / score
======== STOP 2H ========
No broker. No live feed. No PaperLedger. No Risk Guard writes.
```

## v1 locks

| Knob | Value |
|---|---|
| 2C policy | unchanged: nearest WEEKLY, `allow_same_day=false` |
| Dataset | keeps the full historical contract universe |
| Mapping | fixture/eval `EXACT`; `NEAREST_WITHIN_TOLERANCE` reserved |
| Eval sample | `grow.history.eval.sample.v1` — `FRAMEWORK_TEST_ONLY` |
| APPROVED_FOR_2E | requires licensed HISTORICAL_RESEARCH, not fixture |
| Cadence | eval sample 11:00 and 15:15 IST |

## Qualification states

`CANDIDATE` → `QUALIFIED` / `QUALIFIED_WITH_WARNINGS` / `REJECTED` →
`APPROVED_FOR_2E` or `RETIRED`.

A technically passing framework sample **cannot** become `APPROVED_FOR_2E`.
2F freeze of real historical data requires `QUALIFIED`,
`QUALIFIED_WITH_WARNINGS` (with explicit warning IDs), or `APPROVED_FOR_2E`.

## Nearest weekly

`select_nearest_weekly_expiry` reconstructs the universe at `as_of` and
applies the locked rule. Decision-time selection remains
`grow.options.select.choose_expiry` on the reconstructed chain.

Milestone 2I adds a versioned `IndexUniverseRegistry` and
`resolve_nearest_expiry` so weekly-preferred and monthly-only indices can
coexist without hard-coding NIFTY/BANKNIFTY as the only possible names.
See [`dynamic.md`](dynamic.md).

