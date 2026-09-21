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
HMAC-SHA256 over proposal identity fields. There is **no published default
secret**. `GROW_RISK_SECRET` must be set, or tests must inject a secret into
`RiskGuard(..., secret=...)`. Known values such as `grow-risk-v1-paper-only`
are refused. A CEO, a test, or a dashboard cannot forge a stamp without that
process secret.

## Cash long-only

`OPEN+SELL` is a short. Risk Guard rejects it (`policy.long_only`). The
ledger refuses negative inventory even if a stamp is missing or forged.
`SELL` is legal only as flatten/reduce/square-off of an existing long.

## Square-off

During `15:15`–`15:30` IST the session state is `SQUARE_OFF_WINDOW`.
Risk Guard:

- rejects `Intent.OPEN`
- allows `Intent.CLOSE` / `REDUCE` / `SQUARE_OFF`

This is the interface for forced intraday flatten. Persistence of working
orders and a scheduler that *initiates* square-off are later work. The rule
is already enforced so those features cannot "forget" it.

## What this does not guarantee

- It does not make paper P&L realistic (quotes are stubs).
- It does not replace SEBI / exchange obligations.
- It does not survive a future commit that sets `LIVE_TRADING_COMPILED = True`
  without a dedicated architecture review.
