# Milestone 2E — Fixture backtest framework

Status: **fixture backtest framework implemented.**  
This is **not** historical profitability validation. Real point-in-time
underlying + option-chain datasets (and an exchange session calendar) are
required before any performance conclusion. Passing 2E does **not**
authorize live trading.

```
Fixture OHLCV + fixture option chain
        ↓
2A snapshot (as_of prefix only)
        ↓
2B StrategyEngine
        ↓
2C IndexOptionsEngine
        ↓
2D fixture CEO **or recorded CEODecision**
        ↓
ExecutionSimulator  BUY at ask + slippage
        ↓
Session square-off 15:15  (sell at bid − slippage)
        ↓
Isolated BacktestLedger  ≠ PaperLedger
        ↓
Metrics + walk-forward TEST windows (no calibration)
```

## Locked v1 policy

| Knob | Value |
|---|---|
| Data | fixture only |
| Fill | **ask + slippage**; no ask → no fill |
| Strict | LTP-only rejected |
| Same-bar SL/TP | **stop first**, tagged `STOP_FIRST_AMBIGUOUS` |
| Exit | 15:15 IST square-off (or earlier stop/target) |
| Quantity | **lots**; `lot_size` is the contract multiplier |
| P&L | `(exit − entry) × lot_size × lots` |
| Walk-forward | TRAIN/VALIDATE/TEST windows; **`calibration_mode=NONE`** |
| Trainable parameters | `()` — v1 does not tune |
| Costs | `costs.india.fn_o.v1` (brokerage, STT sell, exchange, GST) |
| Slippage | `slip.ask.v1` (bps of premium) |

Zero-cost is not the primary result. Gross and net are both reported.

## Calendar

`nse.weekday.v1` (`WeekdayFixtureCalendar`) is **fixture/unit tests only**.
It is not an NSE holiday calendar. A future historical-data run must pass
`ExplicitSessionCalendar` with exchange session dates. Do not treat every
weekday as a trading session.

## Recorded AI

If `recorded[packet_id]` is supplied, that `CEODecision` is used and
`ResearchOrchestrator.run()` is **not** called. The existing
`DecisionValidator` still gates the recorded output. Missing key → `NO_TRADE`.

## Leakage

Decision `as_of` is the only clock. Underlying bars with `end > as_of` and option snapshots with `as_of > decision` are rejected. Exit simulation may read **later** snapshots after approval; those bars never enter 2B/2C/2D.

## Walk-forward

`TRAIN | embargo | VALIDATE | embargo | TEST`, then step. Primary metrics are
the concatenated **test** windows only. Train/validate are recorded for
structure; they are not used to select parameters. `calibrate_on_test=true`
raises `TEST_WINDOW_TUNING`.

Ablations (same period): `always_no_trade`, `skip_ceo` (2B+2C auto-approve), `full`.

## Isolation

`grow.backtest` does not import `grow.paper` or `grow.risk`. It does not call a broker. Dashboard label: **HISTORICAL RESEARCH / NOT LIVE**.
