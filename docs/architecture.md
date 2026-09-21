# Grow architecture (milestone 1)

Grow is an Indian-market multi-agent **research and paper-trading** system.
It is not a fork of TradingAgents, and it is not a live broker.

This document is the contract for **Architecture Review #1**. Code that
disagrees with this file is a bug.

## Product split

```
tradingagents/     clean-room research graph (roles + bundle)
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
                            (TradeProposal only)
                                    │
                                    ▼
                              Risk Guard
                         (deterministic ruleset)
                            mint RiskStamp
                                    │
                                    ▼
                             Paper ledger
                           (GROW_PAPER venue)
```

Illegal arrows, all tested:

- CEO → paper ledger (no stamp)
- CEO → live broker
- graph → paper ledger
- config/env → live mode
- forged `RiskStamp` → paper ledger

## Modules

| Path | Milestone 1 status | Responsibility |
|---|---|---|
| `grow/execution/lock.py` | **implemented** | Compile-time + env + runtime paper lock |
| `grow/execution/live.py` | **implemented (refuse)** | Live broker surface that only raises |
| `grow/config.py` | **implemented** | YAML + env overlay, fail-closed |
| `grow/model_gateway/` | **implemented (mock)** | Provider abstraction; remote = later |
| `grow/ceo/` | **implemented** | Proposal only |
| `grow/market/` | **implemented (stub quotes)** | Brief + NSE session/square-off clock |
| `grow/risk/` | **implemented** | Deterministic guard + HMAC stamp |
| `grow/paper/` | **implemented** | In-memory ledger |
| `grow/cycle.py` | **implemented** | The one legal orchestration path |
| `tradingagents/` | **foundation** | Roles, default_config, sequential graph |
| `grow/options/` | interface only | F&O later |
| `grow/strategies/` | interface only | Strategy book later |
| `grow/data/` | interface only | Licensed feeds later |
| `grow/learning/` | interface only | Memory/reflection later |
| `grow/dashboard/` | snapshot schema | Web console later |

## Why the guard is not an LLM

LLM risk committees are useful as commentary. They are not a gate.
Grow's Risk Guard is pure functions over numbers, session, and universe.
It does not call the model gateway. The CEO *may* be an LLM (today: mock).
The stamp is HMAC over proposal fields so the ledger cannot be talked into
a fill.

## Indian market assumptions (explicit, provisional)

- Timezone `Asia/Kolkata`
- Cash session `09:15`–`15:30` IST
- Forced square-off window `15:15`–`15:30` IST (new entries blocked; flatten allowed)
- Exchange tag `NSE`
- Currency `INR`
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

## Deferred on purpose

Options, live quotes, FII/DII, promoter pledging, scraping, broker adapters,
LangGraph debate loops, persistence, and a production dashboard are **out of
scope** for this commit. Existing Indian TradingAgents projects already
explore some of those; Grow will borrow *ideas* after independent validation,
not their HTML parsers or cookie-based feeds.
