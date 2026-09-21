# Milestone 1 checklist

Shipped in the first commit:

1. TradingAgents foundation (`tradingagents/`) — roles, default config, sequential graph
2. Grow project tree (`grow/` with CEO, market, options, strategies, risk, execution, paper, data, learning, model_gateway, dashboard)
3. Configuration system (`configs/grow.default.yaml` + env overlay)
4. Model gateway abstraction (mock default)
5. CEO interface (proposal only)
6. Risk Guard interface (deterministic + HMAC stamp)
7. Market Research interface (stub brief + NSE session)
8. Paper-trading-only safety lock (compile + boot + runtime)
9. Automated tests (`python -m unittest discover -s tests -v`)
10. Architecture documentation (`docs/architecture.md`, `docs/safety.md`)

Explicitly **not** in this commit:

- Options chains / greeks
- Live or scraped NSE/BSE/FII data
- Broker SDKs
- LangGraph upstream adapter
- Persistent storage
- Production web dashboard (schema only)

## How to review

1. Read `docs/architecture.md` and `docs/safety.md`.
2. Run the test suite.
3. Try to break the lock: `GROW_EXECUTION_MODE=live python -m grow` (it must refuse).
4. Run `python scripts/run_paper_cycle.py RELIANCE` and confirm fills, if any, have `venue_id=GROW_PAPER`.
5. Do not start options/data/execution work until this review passes.
