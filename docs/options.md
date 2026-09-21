# Milestone 2C — index options engine

Status: **implemented. Research only.**  
2C stops at `OptionsDecision`. It does not execute, size a position, or call a broker.

```
StrategySignal (BULLISH | BEARISH)
      + MarketSnapshot
      + OptionChainSnapshot (fixture)
      ↓
IndexOptionsEngine
      ↓
BUY CE candidate | BUY PE candidate | NO TRADE
======== STOP 2C ========
2D CEO → Risk Guard → paper
```

## Locked policy (v1)

| Knob | Value |
|---|---|
| Underlyings | NIFTY, BANKNIFTY |
| Types | CE, PE |
| Intent | BUY only |
| Mapping | BULLISH → CE, BEARISH → PE |
| Same-day expiry | **forbidden** |
| Expiry class | nearest **weekly** |
| Strike window | ATM ± 2 chain steps |
| Missing IV / Greeks | eligible, those score components = 0 |
| Provider | fixture only |

Live chains, stock options, futures, and option selling are rejected.

## Mapping

Research language, not execution:

- `BULLISH` → candidate **BUY CE**
- `BEARISH` → candidate **BUY PE**

`SHORT`, `SELL`, `OPTION_SELL`, `LONG`, `BUY` as *signal direction* are rejected.
A BEARISH result is not a short sale of the index.

## Chain contract

`OptionChainSource.snapshot(underlying, as_of, spot=...)` returns
`OptionChainSnapshot`. 2C ships `FixtureOptionChain` only.
`OptionsDesk.chain` still raises — that is the vendor door, and it stays shut.

Timestamps are timezone-aware `Asia/Kolkata`. Quotes with `timestamp > as_of`
are future data and are rejected. Chains older than
`options.freshness.max_chain_age_minutes` (default 5) are `NO_TRADE`.

Duplicate identity `(underlying, expiry, strike, type)` is rejected.
Bid/ask cannot be negative; ask < bid is a crossed quote.

## Expiry & strikes

Expiries come from the chain. None are invented. Same-day weekly is skipped.
The soonest remaining **weekly** expiry wins. If none, `NO_TRADE`.

ATM is the listed strike nearest to the **current** snapshot spot. The window
is that ATM index ± 2 in the sorted unique strikes (step inferred from the
chain, not hard-coded 50/100 in the engine). Cheap far-OTM is out of window.

## Liquidity

Configurable: `min_volume`, `min_open_interest`, `max_spread_pct`.
Failing a threshold rejects the contract; it does not force a different BUY.

## Scoring (`options.score.v1`)

Not P(profit):

```
total = Σ weight_i * component_i   # weights sum to 1, each component in [0, 1]
```

| Component | Evidence |
|---|---|
| direction_alignment | 1 after the CE/PE gate |
| moneyness_suitability | ATM 1.0, ITM 0.70, OTM 0.45 |
| liquidity / OI / volume | vs configured floors |
| spread_quality | 1 − spread_pct / max_spread_pct |
| iv_suitability | 0 if missing; else closeness to 18% |
| greek_suitability | 0 if missing; else closeness to ±0.40 delta |
| time_to_expiry | 1–10 days preferred; same-day impossible |
| data_freshness | age vs max_chain_age |

Tie-break: total, then OI, then volume, then tighter spread, then stable
`(underlying, expiry, strike, type)` ordering.

## NO TRADE

First-class. Typical diagnostics: `STALE_CHAIN`, `NO_ELIGIBLE_EXPIRY`,
`NO_LIQUID_CONTRACT`, `DIRECTION_GATE`, `UNSUPPORTED_UNDERLYING`,
`CHAIN_FROM_FUTURE`, `EXECUTION_LANGUAGE`.

A 2B signal does not guarantee a candidate.

## Intrinsic / extrinsic

```
CE intrinsic = max(spot − strike, 0)
PE intrinsic = max(strike − spot, 0)
extrinsic    = premium − intrinsic
```

Premium is bid/ask mid when both exist, else LTP. Large negative extrinsic
rejects the quote. IV/Greeks are never invented.

## Not in 2C

Broker, live chain, paper fill, position size, daily loss, CEO, LLM.
`IndexOptionsEngine` has no `place_order` / `buy` / `sell`.
