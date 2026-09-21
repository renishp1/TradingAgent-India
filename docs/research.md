# Milestone 2D — AI research / CEO

Status: **implemented. Research / decision intent only.**  
2D stops at `CEODecision`. It does not execute, stamp risk, or write the ledger.

```
2A ResearchView + 2B StrategySignal + 2C OptionsDecision
        ↓
ResearchPacket (immutable)
        ↓
Bull / Bear / Quant / Risk-context  (fixture agents)
        ↓
CEOAgent.synthesize
        ↓
DecisionValidator  (fail closed)
        ↓
TRADE_APPROVE | NO_TRADE
======== STOP 2D ========
later: Risk Guard → paper
```

## Locked v1 policy

| Knob | Value |
|---|---|
| Provider | `fixture` only |
| Candidate | **the 2C winner only** — approve or reject, never invent |
| Mapping | BULLISH → BUY CE, BEARISH → BUY PE |
| Material disagreement | Quant **OPPOSE** or Risk-context **OPPOSE** → `NO_TRADE` |
| Bull vs Bear conflict | preserved on the decision; does **not** auto-kill |
| `INSUFFICIENT_DATA` | any of Bull/Bear/Quant/Risk-context → `NO_TRADE` |
| Confidence gating | off (`ai.confidence.enabled: false`) |
| Execution | never |

The cash `grow.ceo.CEO.propose` path is unchanged (Milestone 1 probe). 2D lives in `grow.research`.

## ResearchPacket

Deterministic `packet_id` from snapshot / signal / candidate / schema.
Timestamps are `Asia/Kolkata`. The packet is frozen. LLM payloads go through
`assert_research_payload` (no OHLCV keys). Candidate `volume` is exposed as
`option_volume`.

## Agents

Provider-neutral `ResearchAgent.research(packet) -> ResearchReport`.
Fixture agents read only packet fields. No broker, shell, filesystem, or
network tools. Prompt versions are `v1` and must bump when the template
changes.

Stance: `BULLISH | BEARISH | NEUTRAL | INSUFFICIENT_DATA`  
Recommendation: `SUPPORT | OPPOSE | ABSTAIN`

## CEO

May `TRADE_APPROVE` the supplied `option_candidate_id` or `NO_TRADE`.
May not change strike, expiry, type, premium, or identity.
May not emit quantity, broker, sell, short, or Risk Guard bypass fields.

Fixture CEO is **deterministic gates**, not an LLM: 2C CANDIDATE + valid
mapping + no material disagreement / insufficient data → approve.

## Validator

Any failure rewrites the result to `NO_TRADE` with structured reasons.
Includes: schema, snapshot consistency, unknown candidate, BULLISH+PE,
BEARISH+CE, NEUTRAL+candidate, prohibited execution keys, candidate
mutation, 2C NO_TRADE, Quant/Risk OPPOSE, insufficient reports.

## Failure policy

Timeout, provider exception, malformed report → `NO_TRADE`.
No silent provider switching. `max_retries: 0`.

## Audit

Append-only `AuditLog`. No delete/rewrite API. No secrets. Records packet,
reports, decision, prompt versions, snapshot IDs.

## Not in 2D

Broker, live LLM, paper fill, Risk Guard redesign, position sizing,
LangGraph requirement. `ResearchOrchestrator` has no `place_order`.
