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

## Control plane (unchanged)

```
Market research + research graph
            |
            v
           CEO          (TradeProposal only, cash long-only OPEN)
            |
            v
        Risk Guard      (code, HMAC RiskStamp)
            |
            v
       Paper ledger     (GROW_PAPER, no shorts)
```

## Milestone 2 (not started here)

Phase 2A — data layer (OHLCV / indices / later options fields), broker still disconnected.  
Phase 2B — quantitative `StrategySignal` book.  
Phase 2C — LLM research over **signals**, not LLM-as-trader.

Broker execution is after that, under a dedicated review that would have to
flip `LIVE_TRADING_COMPILED`.
