# Milestone 3B — Paper position management & real-time P&L

Status: **open BUY CE/PE paper positions are marked to market from valid 3A snapshots and closed deterministically.**  
No broker. No live orders. No AI exits. No trailing stop.

```
3A validated snapshot
        ↓
PositionRegistry.mark (BID, else LTP; never ASK)
        ↓
Exit evaluator (STOP_LOSS → TAKE_PROFIT → SESSION_CLOSE)
        ↓
Paper CLOSE (Intent.CLOSE / SQUARE_OFF, Venue.PAPER)
        ↓
RiskGuard.evaluate_exit (reduces exposure only)
        ↓
PaperLedger flatten
        ↓
Realized P&L (gross − configured round-trip costs)
        ↓
Existing 3A new-entry path (only if session/feed remain safe)
```

## Locks

| Knob | Value |
|---|---|
| Entry stop | `entry × (1 − stop_loss_pct)` default 20% |
| Entry target | `entry × (1 + take_profit_pct)` default 20% |
| Exit quote | BID, else LTP. Never ASK |
| Lot size / quantity | Captured at OPEN; never recomputed |
| Session square-off | `SessionCalendar` `SQUARE_OFF_WINDOW` / `CLOSED` |
| Persistence | In-memory |
| Trailing stop | forbidden |
| Broker / live trading | forbidden |

Missing or stale/future/out-of-order quotes do not update marks and cannot fabricate an exit.
A SESSION_CLOSE without a valid quote records `UNRESOLVED_CLOSE`, halts new entries, and degrades the session.
A session timeout with OPEN inventory records `SESSION_TIMEOUT_WITH_OPEN_POSITION`, preserves last valuation, does not fabricate a close, stops the provider, and blocks new entries.

## P&L accounting

| Metric | Source |
|---|---|
| `gross_realized_pnl` | Fill-price P&L (`(exit − entry) × quantity`); matches PaperLedger `realized_pnl` |
| `total_costs` | Existing `CostModel.round_trip` (not embedded in the ledger book) |
| `net_realized_pnl` | `gross_realized_pnl − total_costs` |
| Risk Guard `daily_pnl` | **net** realized P&L for the paper session, never ledger gross |
