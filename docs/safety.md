# Safety lock

Grow milestone 1 cannot trade live money. That is a product invariant, not a
setting.

## Three layers

1. **Compile-time.** `LIVE_TRADING_COMPILED = False` in
   `grow/execution/lock.py`. Tests assert the source contains that assignment
   and does not contain `= True`.
2. **Boot-time.** `load_config()` and `inspect_environment()` reject:
   - `GROW_EXECUTION_MODE` other than `paper`
   - `GROW_LIVE_TRADING` / `LIVE_TRADING_ENABLED` truthy
   - broker token env vars (`KITE_ACCESS_TOKEN`, …)
   - `execution.live_trading_enabled: true` in YAML
3. **Runtime.** `assert_paper_runtime` runs before every fill. The `Venue`
   enum has a single member: `PAPER`. `LiveBroker` and `place_live_order`
   exist only to raise `GrowLiveTradingDisabled`.

## Risk stamp

`PaperLedger.submit` requires a `RiskStamp` minted by `RiskGuard`. Tokens are
HMAC-SHA256 over the canonical fill-relevant proposal fields
(`grow.risk.stamp.v2`):

```
proposal_id, symbol, exchange, side, intent, quantity,
limit_price, stop_loss, take_profit, notional, venue, ruleset
```

Thesis and confidence are commentary and are **not** bound. Mutating SL / TP /
notional / size after the stamp was issued fails verify. There is **no
published default secret**. `GROW_RISK_SECRET` must be set, or tests must
inject a secret into `RiskGuard(..., secret=...)`. Known values such as
`grow-risk-v1-paper-only` are refused.

A stamp is necessary, not sufficient. The ledger still checks fill
invariants (quantity, price, notional = qty×price, venue, long-only,
flatten qty ≤ open position) before applying a fill.

## Cash long-only

`OPEN+SELL` is a short. Risk Guard rejects it (`policy.long_only`). The
ledger refuses negative inventory even if a stamp is missing or forged.
`SELL` is legal only as flatten/reduce/square-off of an existing long.

## Square-off

During `15:15`–`15:30` IST the session state is `SQUARE_OFF_WINDOW`.
Risk Guard:

- rejects `Intent.OPEN`
- allows `Intent.CLOSE` / `REDUCE` / `SQUARE_OFF` while the session is
  `OPEN` or `SQUARE_OFF_WINDOW`
- rejects flatten at `15:30` and later (`CLOSED`)

`PaperLedger.square_off_proposal` returns `None` when there is no long to
flatten. A SQUARE_OFF whose quantity exceeds the open position is refused
by the ledger even if the stamp verifies.

Persistence of working orders and a scheduler that *initiates* square-off
are later work. The rule is already enforced so those features cannot
"forget" it.

## Daily P&L (known limitation)

`GrowRuntime` currently passes `book.realized_pnl` into Risk Guard as
`daily_pnl`. That value is **lifetime realized-at-cost** of the in-memory
book, not a trading-day accumulator.

```
Day 1: -₹10,000
Day 2: +₹5,000
realized_pnl = -₹5,000   ← this is what loss.daily sees today
```

The book snapshot records this explicitly:

- `pnl.true_daily_pnl = null`
- `pnl.fed_to_risk_guard_as = lifetime_realized_pnl`

Do not treat `loss.daily` as a real daily halt until Milestone 2A adds a
session-day accumulator (`daily_realized_pnl`, `daily_unrealized_pnl`,
fees, slippage). The rule still fail-closes when the number it *is* given
breaches `max_daily_loss`.

## What this does not guarantee

- It does not make paper P&L realistic (quotes are stubs).
- It does not replace SEBI / exchange obligations.
- It does not survive a future commit that sets `LIVE_TRADING_COMPILED = True`
  without a dedicated architecture review.
- The 2026 holiday seed is **not** the official exchange circular. Do not
  attach a live data feed until that calendar is replaced.
