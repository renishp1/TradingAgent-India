# Grow / TradingAgent-India

Indian-market multi-agent trading **research** system. **Paper-trading only.**

This repository is the source of truth for Grow. Milestone 1 passed
**Architecture Review #1** ([`docs/review-1.md`](docs/review-1.md)). It is
still a constrained paper-trading foundation, not a desk.


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

Brokers, live order placement, and LangGraph remain **out of scope**.
Milestone 2A is fixture market data ([`docs/milestone-2a.md`](docs/milestone-2a.md)).
Milestone 2B is the quantitative strategy engine ([`docs/strategy.md`](docs/strategy.md)).
Milestone 2C is the index-options research engine ([`docs/options.md`](docs/options.md)).
Milestone 2D is AI research / CEODecision ([`docs/research.md`](docs/research.md)).
Milestone 2E is fixture backtest + walk-forward ([`docs/backtest.md`](docs/backtest.md)).
Milestone 2F is the research director ([`docs/director.md`](docs/director.md)).
Milestone 2G is point-in-time historical data ([`docs/history.md`](docs/history.md)).
Milestone 2H is provider evaluation ([`docs/provider.md`](docs/provider.md)).
Milestone 2I is dynamic index/expiry discovery ([`docs/dynamic.md`](docs/dynamic.md)).
Milestone 2J is recorded historical provider integration ([`docs/integrate.md`](docs/integrate.md)).
Milestone 3A is live market-data → paper trading ([`docs/live_data.md`](docs/live_data.md)).
Milestone 3B is paper position management and real-time P&L ([`docs/positions.md`](docs/positions.md)).
Milestone 3C is provider-neutral live market data (current real provider:
Zerodha/Kite quotes; TrueData adapter is historical/legacy —
[`docs/truedata.md`](docs/truedata.md)).
Milestone 4B is specialist agents & orchestration
([`docs/milestone-4b.md`](docs/milestone-4b.md); canonical `AnalysisOrchestrator`).
Do not attach a live broker or real-money execution.


## Layout

```
TradingAgent-India/
├── tradingagents/          # research-graph foundation
├── grow/
│   ├── ceo/                # cash CEO (M1 probe)
│   ├── research/           # 2D fixture AI research / CEODecision
│   ├── backtest/           # 2E fixture replay + walk-forward
│   ├── director/           # 2F research plan / freeze / review
│   ├── history/            # 2G point-in-time historical data
│   ├── live_data/          # 3A mock stream → paper (no broker)
│   ├── market/
│   ├── options/            # 2C fixture chain; BUY CE/PE research only
│   ├── strategies/         # 2B quant engine (signals only)
│   ├── risk/
│   ├── execution/          # lock + live refuse
│   ├── paper/
│   ├── data/               # 2A fixture OHLCV (no live feed)
│   ├── learning/           # interface only
│   ├── model_gateway/
│   └── dashboard/          # snapshot schema
├── tests/
├── docs/
├── configs/
└── scripts/run_paper_cycle.py
```

## Quick start

Python 3.10+. The paper core is stdlib. The Zerodha market-data smoke declares `websocket-client` and does not import a broker order client.

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
export GROW_RISK_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
python scripts/run_paper_cycle.py RELIANCE
```

Copy `.env.example` only if you need env overlays. Leave live flags false.
`GROW_RISK_SECRET` is required for any process that mints a RiskStamp.
Tests inject their own secret and do not read a default.


## Safety

| Attempt | Result |
|---|---|
| `GROW_EXECUTION_MODE=live` | `GrowLiveTradingDisabled` |
| `GROW_LIVE_TRADING=true` | `GrowLiveTradingDisabled` |
| `LiveBroker()` | `GrowLiveTradingDisabled` |
| Paper fill without RiskStamp | `GrowSafetyError` |
| Mutated SL/TP after stamp | verify fails |
| Flatten qty > position | `GrowSafetyError` |
| `OPEN+SELL` (short) | Risk Guard `policy.long_only` |
| New entries after 15:15 IST | Risk Guard reject |
| Missing `GROW_RISK_SECRET` | `GrowConfigError` |
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

See [`docs/review-1.md`](docs/review-1.md). Next work is Milestone 2A
(data layer), not brokers.

