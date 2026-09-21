# Architecture Review #1 — accepted

Review date: 2026-09-21. Source of truth: GitHub `main`.

Grow remains a **paper-trading research foundation**, not a trading system.
Live brokers stay out. Milestone 2 is **licensed market data + research
engine**, not execution.

## Decisions locked in

| Item | Decision |
|---|---|
| HMAC secret | Required. No published default. Tests inject. Env `GROW_RISK_SECRET`. |
| Position policy | **CASH long-only.** `OPEN+SELL` is a short and is rejected. `SELL` is flatten/reduce only. |
| Paper P&L | Realized-at-cost only. `unrealized_pnl` is `null`. MTM/equity/drawdown wait for real quotes. |
| Concentration | `basis=cost_notional` (`cash + gross notional`). Redesign with MTM equity in a later milestone. |
| CEO SL/TP | Probe placeholder 2%/3%. `stop_source=probe_placeholder`. Strategy engine owns this later. |
| TradingAgents graph | Architecture skeleton. Stub notes on purpose. Not intelligence. |
| Safety lock | Keep all three layers. No broker SDKs. |

## Follow-ups after acceptance (landed)

| Item | Decision |
|---|---|
| RiskStamp binding | Canonical JSON of the fill-relevant proposal (`grow.risk.stamp.v2`) → SHA-256 → HMAC-SHA256. SL, TP, and notional are bound. Thesis/confidence are not. |
| Ledger invariants | Stamp is necessary, not sufficient. Ledger checks qty, price, notional=qty×price, venue, long-only, flatten qty ≤ open position. |
| Square-off tests | Unknown position, oversize flatten, OPEN-session flatten allowed, 15:29 allowed, 15:30+ rejected. |
| Daily P&L | **Known limitation.** `loss.daily` currently sees lifetime `realized_pnl`, not a session-day accumulator. `pnl.true_daily_pnl = null` until 2A valuation. |

## Control plane (unchanged)

```
Market research + research graph
            │
            ▼
           CEO          (TradeProposal only, cash long-only OPEN)
            │
            ▼
        Risk Guard      (code, HMAC RiskStamp of the exact proposal)
            │
            ▼
       Paper ledger     (GROW_PAPER, no shorts, own fill invariants)
```

## Milestone 2 (not started here)

Phase 2A — data layer (OHLCV / indices / later options fields), broker still disconnected.  
Do not attach a live feed until the holiday calendar is the official circular.  
Phase 2B — quantitative `StrategySignal` book.  
Phase 2C — LLM research over **signals**, not LLM-as-trader.

Broker execution is after that, under a dedicated review that would have to
flip `LIVE_TRADING_COMPILED`.
