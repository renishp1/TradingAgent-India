# Grow / TradingAgent-India

Indian-market multi-agent trading **research** system. **Paper-trading only.**

This repository is the source of truth for Grow. Milestone 1 is a foundation
commit for Architecture Review #1 — not a complete desk.

> Not financial advice. Not a broker. Not SEBI-registered. Live execution is
> not compiled into this software.

## What this commit contains

1. `tradingagents/` — clean-room TradingAgents **foundation** (roles, config, sequential research graph). Inspired by [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) (Apache-2.0). **Not a copy of that tree.**
2. `grow/` — firm layout: CEO, market, options, strategies, risk, execution, paper, data, learning, model gateway, dashboard.
3. Configuration (`configs/grow.default.yaml` + env overlay).
4. Model gateway abstraction (deterministic **mock** by default).
5. CEO interface — emits `TradeProposal`, cannot fill.
6. Risk Guard — deterministic rules + HMAC stamp.
7. Market Research — stub brief + NSE session / 15:15 IST square-off window.
8. Paper-trading-only safety lock (compile-time, boot-time, runtime).
9. Automated tests.
10. Architecture docs: [`docs/architecture.md`](docs/architecture.md), [`docs/safety.md`](docs/safety.md), [`docs/milestone-1.md`](docs/milestone-1.md).

Options, live data, brokers, and LangGraph are **out of scope** until the
review passes.

## Layout

```
TradingAgent-India/
├── tradingagents/          # research-graph foundation
├── grow/
│   ├── ceo/
│   ├── market/
│   ├── options/            # interface only
│   ├── strategies/         # interface only
│   ├── risk/
│   ├── execution/          # lock + live refuse
│   ├── paper/
│   ├── data/               # interface only
│   ├── learning/           # interface only
│   ├── model_gateway/
│   └── dashboard/          # snapshot schema
├── tests/
├── docs/
├── configs/
└── scripts/run_paper_cycle.py
```

## Quick start

Python 3.10+. No third-party dependencies in milestone 1.

```bash
python -m unittest discover -s tests -v
python scripts/run_paper_cycle.py RELIANCE
```

Copy `.env.example` only if you need env overlays. Leave live flags false.

## Safety

| Attempt | Result |
|---|---|
| `GROW_EXECUTION_MODE=live` | `GrowLiveTradingDisabled` |
| `GROW_LIVE_TRADING=true` | `GrowLiveTradingDisabled` |
| `LiveBroker()` | `GrowLiveTradingDisabled` |
| Paper fill without RiskStamp | `GrowSafetyError` |
| New entries after 15:15 IST | Risk Guard reject |
| Broker SDK import | not present in the tree |

`LIVE_TRADING_COMPILED` is a constant set to `False`. Environment variables
cannot flip it.

## Design principles

- **Grow is the firm; TradingAgents is a research subsystem.**
- **Risk Guard is code, never an LLM.**
- **No scraped Indian-market HTML in this tree.** Existing forks that scrape
  NSE/BSE/Screener will be treated as architectural references, not as
  sources to copy.
- **Fail closed.** Missing data, high-volatility stub regime, or a closed
  session means no fill — not a guessed quote.

## Review

See [`docs/milestone-1.md`](docs/milestone-1.md). Please treat GitHub as the
source of truth and complete Architecture Review #1 before any
options / data / execution work.
