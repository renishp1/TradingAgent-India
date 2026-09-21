# Milestone 2E — Backtest + walk-forward

Status: **implemented. Historical research only.**  
Passing 2E does **not** authorize live trading.

```
Fixture OHLCV + fixture option chain
        ↓
2A snapshot (as_of prefix only)
        ↓
2B StrategyEngine
        ↓
2C IndexOptionsEngine
        ↓
2D fixture CEO  (or skip_ceo / always_no_trade ablation)
        ↓
ExecutionSimulator  BUY at ask + slippage
        ↓
Session square-off 15:15  (sell at bid − slippage)
        ↓
Isolated BacktestLedger  ≠ PaperLedger
        ↓
Metrics + walk-forward test windows
```

## Locked v1 policy

| Knob | Value |
|---|---|
| Data | fixture only |
| Fill | **ask + slippage**; no ask → no fill |
| Strict | LTP-only rejected |
| Same-bar SL/TP | **stop first**, tagged `STOP_FIRST_AMBIGUOUS` |
| Exit | 15:15 IST square-off (or earlier stop/target) |
| Positions | one at a time, quantity 1 |
| Walk-forward | frozen config; **no test-window tuning** |
| Costs | `costs.india.fn_o.v1` (brokerage, STT sell, exchange, GST) |
| Slippage | `slip.ask.v1` (bps of premium) |

Zero-cost is not the primary result. Gross and net are both reported.

## Leakage

Decision `as_of` is the only clock. Underlying bars with `end > as_of` and option snapshots with `as_of > decision` are rejected. Exit simulation may read **later** snapshots after approval; those bars never enter 2B/2C/2D.

## Walk-forward

`TRAIN | embargo | VALIDATE | embargo | TEST`, then step. Test windows are concatenated for the primary report. No parameter grid. `calibrate_on_test=true` raises `TEST_WINDOW_TUNING`.

Ablations (same period): `always_no_trade`, `skip_ceo` (2B+2C auto-approve), `full`.

## Isolation

`grow.backtest` does not import `grow.paper` or `grow.risk`. It does not call a broker. Dashboard label: **HISTORICAL RESEARCH / NOT LIVE**.
