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
   - broker token env vars (`KITE_ACCESS_TOKEN`, …) when present in the
     *inspect* environ
   - `execution.live_trading_enabled: true` in YAML

   Paper boot **scrubs** broker credential keys from the inspect environ by
   default (`scrub_broker_credentials=True` on `load_config`, helper
   `scrub_broker_credentials_for_paper`). That lets a local `.env` hold Kite
   smoke credentials without blocking paper/dashboard startup. Live-trading
   flags are never scrubbed. Pass `scrub_broker_credentials=False` only for
   intentional credential-presence checks. Smoke scripts read secrets from
   the raw process environ after config load.
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

## Daily P&L

`PaperExecutionEngine` (4C → paper campaign path) feeds Risk Guard with
**IST trading-day** realized P&L. Closes from prior calendar days are excluded.
Open-mark unrealized P&L is included only in the engine's entry-gate
`DAILY_LOSS_LIMIT` check (`trading_day_total_pnl`). A daily-loss halt does not
carry across midnight IST.

`GrowRuntime` (legacy cash CEO probe) still passes lifetime
`book.realized_pnl` into Risk Guard. Its book snapshot records:

- `pnl.true_daily_pnl = null`
- `pnl.fed_to_risk_guard_as = lifetime_realized_pnl`

Do not treat that legacy path as a real daily halt until it adopts the same
trading-day accumulator.

## What this does not guarantee

- It does not make paper P&L realistic (quotes are stubs).
- It does not replace SEBI / exchange obligations.
- It does not survive a future commit that sets `LIVE_TRADING_COMPILED = True`
  without a dedicated architecture review.
- The 2026 holiday seed is **not** the official exchange circular. Do not
  attach a live data feed until that calendar is replaced.
