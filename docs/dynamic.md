# Milestone 2I — Dynamic index / expiry discovery & provider qualification

Status: **architecture + synthetic provider-shaped sample.**  
No licensed NSE / Global Datafeeds / TrueData feed is attached.
This is **not** live data and **not** a profitability claim.

```
Provider-native sample (file)
        ↓
2G CanonicalStore
        ↓
IndexUniverseRegistry (OPTIDX, versioned)
        ↓
discover_underlyings(as_of)
        ↓
ExpiryResolver (WEEKLY_PREFERRED | MONTHLY_ONLY | …)
        ↓
2C IndexOptionsEngine (CE/PE, ATM±2)
        ↓
CandidateSet (versioned multi-underlying extension)
======== STOP 2I ========
2D still single-candidate by default
No broker. No live feed. No PaperLedger. No Risk Guard writes.
```

## What 2I does not do

- It does not rank vendors.
- It does not claim any public candidate is `APPROVED_FOR_2E`.
- NSE / Global Datafeeds / TrueData remain `CANDIDATE` until a licensed
  sample passes the mandatory gates.
- The 2I sample `grow.history.provider.sample.v1` is `FRAMEWORK_TEST_ONLY`
  and **cannot** become `APPROVED_FOR_2E`.

## Index universe

`IndexUniverseRegistry` is the only place new OPTIDX names are added.
NIFTY and BANKNIFTY stay weekly-preferred defaults. MIDCPNIFTY is registered
as `MONTHLY_ONLY` for this sample period. RELIANCE/TCS/stock options and
futures are rejected.

Discovery at `as_of` uses contracts actually listed in the dataset. The
`IndexUniverseRegistry` is a versioned **policy/approval overlay**, not the
source of which symbols exist. A new OPTIDX name can appear in a provider
sample and become eligible by adding a registry policy — without changing
the provider adapter. Unapproved or stock/futures names stay rejected.
An index in the registry but absent from the contract master is
`DATA_UNAVAILABLE`, not invented.

## Expiry policy profiles

| Profile | Rule |
|---|---|
| `WEEKLY_PREFERRED` | nearest future weekly; no same-day; no silent monthly fallback |
| `MONTHLY_ONLY` | nearest future monthly; no same-day |
| `WEEKLY_THEN_MONTHLY` | weekly first; monthly only if this profile is configured |
| `CUSTOM_HISTORICAL` | reserved for dated exchange-rule changes |

`resolve_nearest_expiry` is the audit resolver. Decision-time selection is
still `choose_expiry` on the reconstructed chain, with the same profile.
Per-index `strike_policy_profile` (`ATM_PM0`–`ATM_PM3`) sets the 2C strike
window. `lot_size_source=CONTRACT_MASTER` requires a historical lot size on
the **canonical** selected contract (resolved by underlying/expiry/strike/
option type, not by `provider_contract_id`). Unknown profiles fail closed.
2C does not invent strikes or expiries.

### Liquidity policy (v1)

`options.select.v1` is the **single locked liquidity policy**. Per-index
`liquidity_policy` is recorded on `IndexPolicy` and bound into
`policy_fingerprint` / CandidateSet provenance. It does **not** change the
2C liquidity algorithm in v1 (`min_volume`, `min_open_interest`,
`max_spread_pct` stay the locked OptionsConfig gates). Additional named
liquidity profiles are reserved for a later milestone.

## CE / PE

Unchanged: BULLISH → BUY CE, BEARISH → BUY PE. No option selling.

## Qualification

`ProviderEvaluationRunner` plus `DatasetQualificationStore`. Records bind
`dataset_id + version + fingerprint`. A correction is a new fingerprint.

## Vendor request (unchanged from the requirements)

Ask each vendor for a sample covering NIFTY/BANKNIFTY plus one additional
index, contract master, listing windows, bid/ask/LTP/OI/volume, lot size,
timezone/snapshot semantics, and research-license terms. Do not treat a
marketing page as a passing gate.
