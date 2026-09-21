# Grow architecture (milestone 1, Review #1 accepted)

Grow is an Indian-market multi-agent **research and paper-trading** system.
It is not a fork of TradingAgents, and it is not a live broker.

This document is the contract. Code that disagrees with this file is a bug.
Review #1 findings live in [`docs/review-1.md`](review-1.md).
Milestone 2A (data plane, fixture only) lives in [`docs/milestone-2a.md`](milestone-2a.md).

## Product split

```
tradingagents/     clean-room research graph (roles + bundle) — skeleton
grow/              the firm: CEO, market, risk, paper, model gateway
```

Upstream TradingAgents (TauricResearch, Apache-2.0) is an inspiration for
desk-style agent roles. Grow does **not** vendor that repository. A later
milestone may add an optional adapter that wraps LangGraph if installed.
The adapter would still emit a `ResearchBundle`. It would never stamp risk
or talk to a broker.

## Control plane

```
                  Market Research          TradingAgents graph
                         │                         │
                         └──────────┬──────────────┘
                                    ▼
                                   CEO
                         (TradeProposal, cash long-only)
                                    │
                                    ▼
                              Risk Guard
                         (deterministic ruleset)
                            mint RiskStamp
                                    │
                                    ▼
                             Paper ledger
                           (GROW_PAPER, no shorts)
```

Illegal arrows, all tested:

- CEO → paper ledger (no stamp)
- CEO → live broker
- graph → paper ledger
- config/env → live mode
- forged `RiskStamp` → paper ledger
- `OPEN+SELL` → short inventory

## Position policy (cash)

Milestone 1 product is `CASH`. `allow_short` must be false.

| Intent | Side | Meaning |
|---|---|---|
| OPEN | BUY | Open / add long |
| OPEN | SELL | **Rejected** — that is a short |
| CLOSE / REDUCE / SQUARE_OFF | SELL | Flatten or reduce a long |
| CLOSE / REDUCE / SQUARE_OFF | BUY | Illegal on a long-only book (no short to cover) |

The CEO emits only `BUY+OPEN` probes. Risk Guard still rejects `OPEN+SELL`.
The stamp binds the whole fill-relevant proposal. The ledger refuses
negative quantity, notional mismatches, and flatten qty that exceeds the
open long even if a stamp were presented.

## Valuation (honest)

Paper book reports `valuation.method = cost_notional`. `unrealized_pnl` is
`null`. Concentration uses `cash + gross_notional`, labeled
`basis=cost_notional`. Do not treat this as strategy performance. MTM equity
and drawdown wait for licensed quotes.

`loss.daily` is **not a true daily P&L** yet. Runtime feeds the Risk Guard
`book.realized_pnl` (lifetime realized-at-cost of the in-memory book). The
snapshot field `pnl.true_daily_pnl` is `null` until a session-day
accumulator exists. Documented in [`safety.md`](safety.md).

## RiskStamp (exact proposal)

The HMAC payload is canonical JSON (`grow.risk.stamp.v2`) of:

`proposal_id, symbol, exchange, side, intent, quantity, limit_price,
stop_loss, take_profit, notional, venue, ruleset`

SHA-256 of that JSON, then HMAC-SHA256. Changing SL/TP/notional after the
gate mints a stamp makes verify fail. The ledger additionally checks fill
invariants so a stamp is never the only check.

## Data plane (Milestone 2A)

Accepted control plane is unchanged. 2A adds a **fixture** data plane that is
not yet wired into `GrowRuntime`:

```
Raw fixture OHLCV  →  Normalizer  →  MarketSnapshot  →  ResearchView
                                              ↓
                                    (2B) StrategySignal
```

LLM research must not receive raw candles. Licensed feeds, scrapes, and
broker quotes are refuse-closed until a named vendor is reviewed.

## Modules

| Path | Status | Responsibility |
|---|---|---|
| `grow/execution/lock.py` | **implemented** | Compile-time + env + runtime paper lock |
| `grow/execution/live.py` | **implemented (refuse)** | Live broker surface that only raises |
| `grow/config.py` | **implemented** | YAML + env overlay, fail-closed |
| `grow/model_gateway/` | **implemented (mock)** | Provider abstraction; remote = later |
| `grow/ceo/` | **implemented** | Proposal only; placeholder SL/TP |
| `grow/market/` | **implemented (stub quotes)** | Brief + NSE session/square-off clock |
| `grow/risk/` | **implemented** | Deterministic guard + HMAC stamp (secret required) |
| `grow/paper/` | **implemented** | In-memory long-only ledger |
| `grow/cycle.py` | **implemented** | The one legal orchestration path |
| `tradingagents/` | **foundation / stub intelligence** | Roles, default_config, sequential graph |
| `grow/data/` | **2A fixture** | OHLCV snapshot, quality, universe. No live feed |
| `grow/strategies/` | **2B type only** | `StrategySignal` frozen; book raises |
| `grow/options/` | interface only | F&O later |
| `grow/learning/` | interface only | `DecisionRecord` type; store deferred |
| `grow/dashboard/` | snapshot schema | Web console later |


## Why the guard is not an LLM

LLM risk committees are useful as commentary. They are not a gate.
Grow's Risk Guard is pure functions over numbers, session, universe, and
position policy. It does not call the model gateway. The CEO *may* be an LLM
(today: mock). The stamp is HMAC over the canonical proposal JSON so the
ledger cannot be talked into a fill of a *different* ticket. The HMAC secret
is required; there is no published default.

## Indian market assumptions (explicit, provisional)

- Timezone `Asia/Kolkata`
- Cash session `09:15`–`15:30` IST
- Forced square-off window `15:15`–`15:30` IST (new entries blocked; flatten allowed)
- Exchange tag `NSE`
- Currency `INR`
- Product `CASH` long-only
- Holiday seed for 2026 is a short list, **not** the official circular

These must be re-validated before any later data/execution milestone.

## Model gateway

Default provider is `mock`. `xai` / `openai` / `anthropic` / `ollama` names
exist so config can point at them later. In milestone 1 they refuse without
a key and still raise `GrowInterfaceNotImplemented` if a key is present.

No model is allowed to:

- set `execution.mode`
- mint a `RiskStamp`
- import a broker
- open a short

## Deferred on purpose

Milestone 2A is **fixture market data**. Milestone 2B is quantitative
`StrategySignal`. Milestone 2C is LLM research over **signals**, not
LLM-as-trader. Options chains, scraping, broker adapters, LangGraph,
persistence, and a production dashboard stay out until those reviews.

