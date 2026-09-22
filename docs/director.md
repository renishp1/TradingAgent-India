# Milestone 2F — CEO Research Director

Status: **fixture research-governance framework.**  
2F does **not** execute trades, does **not** authorize live trading, and
does **not** mark fixture 2E results `ACCEPT_FOR_PAPER`.

```
Approved catalog + 2A–2E capabilities
        ↓
FixtureDirector.plan()
        ↓
ResearchPlanValidator
        ↓
FixtureDirector.freeze()     ← test still blind
        ↓
BacktestCoordinator.run()    ← explicit; not inside freeze
        ↓
FixtureDirector.review()
        ↓
HOLD (fixture) | REJECT (leakage / incomplete)
        ↓
next_plan()  new plan_id
======== STOP 2F ========
```

## Locked v1

| Knob | Value |
|---|---|
| Provider | fixture |
| Approved dataset | `grow.data.fixture.v1` |
| Rejection stub | `grow.data.unapproved.stub.v1` (`NOT_APPROVED`) |
| Config candidate | `grow.default.v1` only |
| Calibration | `NONE` |
| 2E run | `BacktestCoordinator.run(frozen_plan)` after freeze |
| Fixture review | **never** `ACCEPT_FOR_PAPER` |
| Permissions | broker/live/paper/ledger/Risk Guard writes all false |
| Operation flags | `enabled`, `allow_plan_creation`, `allow_plan_freeze`, `allow_result_review` |
| Test visibility | blind until freeze |
| Leakage | from 2E: `CLEAN` / `LEAKAGE` / `UNKNOWN` — coordinator does not invent CLEAN |
| Timestamps | `created_at` = plan event (injected clock); `test_freeze_at` = freeze event |

`ACCEPT_FOR_PAPER` means “eligible for a later paper-evaluation milestone”,
not live trading. It is not issued against synthetic fixture data.

Coordinator runs the frozen **test_window** (and records train/validate/embargo/config
from the plan). It does not substitute global 2E walk-forward defaults.

## Freeze / blindness

After `FROZEN`, test windows, candidates, metrics, and acceptance rules
cannot be edited in place. A change produces a **new** `plan_id`.
`review()` on a non-frozen plan raises `TEST_BLIND_UNTIL_FROZEN`.
Coordinator rejects a plan whose fingerprint ≠ `freeze_hash`.

## Calendar

Plans inherit the dataset’s `session_calendar_version`. Fixture catalog
uses `nse.weekday.v1` (fixture only). Historical validation still needs
`ExplicitSessionCalendar` plus an approved PIT dataset.
