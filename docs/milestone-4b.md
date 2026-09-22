# Milestone 4B — Specialist Agents & Orchestration

Paper-only intelligence layer on the 4A multi-agent foundation.
Not a live trading milestone. No broker order path.

## Architecture

```
Normalized Market Snapshot (immutable snapshot_id + version)
        ↓
Market Data | Technical | Options | Regime | Strategy Research
        ↓
Output validation (schema, snapshot identity, cycle id)
        ↓
Conflict-preserving aggregation
        ↓
AggregateAnalysisPackage → 4C
```

## Packages

| Area | Path |
|------|------|
| Specialist agents | `grow/agents/{market_data,technical,options,regime,strategy_research}/` |
| Orchestration | `grow/orchestration/` (`cycle`, `dispatcher`, `validator`, `aggregator`, `models`) |
| Contracts | `grow/decision/contracts/agent_result.py` (`grow.agent.result.v2`) |
| Snapshot | `grow/market_data/` (unchanged contract; fixture diagnostics may carry `history_closes`) |

## Agent contract (v2)

Every specialist returns:

- identity: `agent_name`, `agent_version`, `cycle_id`, `snapshot_id`, `snapshot_version`
- status: `PASS` / `DEGRADED` / `NO_DATA` / `ERROR`
- separated fields: `observations` (facts), `calculated_metrics`, `interpretation`, `findings`, `assumptions`, `evidence`, `data_quality_concerns`
- timing: `execution_time_ms`
- research-only `candidate_action` (never execution)

Agents never fetch newer market data, fabricate missing values, place orders, or modify Risk Guard limits.

## Orchestration guarantees

1. One `analysis_cycle_id` per run over exactly one snapshot version.
2. Snapshot quality gate can stop the cycle before dispatch.
3. Dispatch validates every output; snapshot mismatches are rejected and recorded.
4. Timeouts/errors mark the agent unavailable — no fabricated substitute findings.
5. Conflicts are preserved (`DIRECTION_CONFLICT`, action/instrument conflicts).
6. Aggregate package is digest-stable for fixed inputs and replayable from the stored snapshot.
7. `paper_mode=true`, `live_trading=false`, `broker_order_path=false`.

## Safety

- `AgentCycleOrchestrator` still consumes the Risk Guard adapter for fail-closed paper safety.
- 4B does **not** open paper positions or enable live trading.
- Final decision workflow / Risk Guard authority integration is **4C**.

## How to run

```bash
python3 -m pytest tests/test_agent_*.py tests/test_orchestration_4b.py -q
python3 scripts/run_agent_cycle.py
```

## Explicit non-goals

No live broker orders, no paper auto-fill, no walk-forward performance claims, no 4C decision integration.
