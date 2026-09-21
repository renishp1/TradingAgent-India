# Milestone 1 checklist

Shipped, then tightened by [Architecture Review #1](review-1.md):

1. TradingAgents foundation (`tradingagents/`) — roles, default config, sequential graph (stub intelligence)
2. Grow project tree (`grow/` with CEO, market, options, strategies, risk, execution, paper, data, learning, model_gateway, dashboard)
3. Configuration (`configs/grow.default.yaml` + env overlay)
4. Model gateway abstraction (mock default)
5. CEO interface (proposal only, cash long-only OPEN)
6. Risk Guard (deterministic + HMAC stamp, secret required)
7. Market Research (stub brief + NSE session)
8. Paper-trading-only safety lock (compile + boot + runtime)
9. Automated tests (`python -m unittest discover -s tests -v`)
10. Architecture documentation

Review #1 follow-ups that landed in this tree:

- HMAC secret fail-closed (no published default)
- Explicit CASH long-only policy (`OPEN+SELL` rejected)
- Valuation labeled `cost_notional` (MTM deferred)
- CEO SL/TP marked `probe_placeholder`

## Not in this milestone

- Licensed OHLCV / options / FII data (that's 2A)
- Strategy engine (2B)
- LLM debate over real signals (2C)
- Broker SDKs / live execution

## How to probe

```bash
export GROW_RISK_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
python -m unittest discover -s tests -v
python scripts/run_paper_cycle.py RELIANCE
```
