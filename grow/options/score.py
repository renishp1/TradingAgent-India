"""Deterministic option-candidate score. Not P(profit). Not an LLM."""

from __future__ import annotations

from datetime import datetime

from grow.config import OptionsConfig
from grow.options.models import OptionContract, OptionType, ScoreBreakdown
from grow.options.select import session_day
from grow.options.validate import moneyness, premium, spread
from grow.strategies.confidence import clamp, unit

SCORING_VERSION = "options.score.v1"

WEIGHTS: dict[str, float] = {
    "direction_alignment": 0.12,
    "moneyness_suitability": 0.12,
    "liquidity_quality": 0.10,
    "spread_quality": 0.12,
    "oi_quality": 0.10,
    "volume_quality": 0.10,
    "iv_suitability": 0.08,
    "greek_suitability": 0.08,
    "time_to_expiry": 0.10,
    "data_freshness": 0.08,
}


def score_contract(
    contract: OptionContract,
    *,
    spot: float,
    atm: float,
    as_of: datetime,
    chain_as_of: datetime,
    option_type: OptionType,
    config: OptionsConfig,
) -> ScoreBreakdown:
    abs_spread, spread_pct = spread(contract)
    kind = moneyness(option_type, spot, contract.strike, atm)
    money = {"ATM": 1.0, "ITM": 0.70, "OTM": 0.45}.get(kind, 0.0)
    liq_oi = unit(contract.open_interest, max(config.min_open_interest * 4, 1))
    liq_vol = unit(contract.volume, max(config.min_volume * 4, 1))
    spread_s = 0.0
    if spread_pct is not None and config.max_spread_pct > 0:
        spread_s = clamp(1.0 - spread_pct / config.max_spread_pct)
    iv_s = 0.0
    if contract.implied_volatility is not None:
        iv_s = clamp(1.0 - abs(contract.implied_volatility - 0.18) / 0.18)
    greek_s = 0.0
    if contract.delta is not None:
        target = 0.40 if option_type is OptionType.CE else -0.40
        greek_s = clamp(1.0 - abs(contract.delta - target) / 0.40)
    days = (contract.expiry - session_day(as_of)).days
    if days <= 0:
        tte = 0.0
    elif days == 1:
        tte = 0.55
    elif days <= 10:
        tte = 1.0
    else:
        tte = 0.50
    age_min = max(0.0, (as_of - chain_as_of).total_seconds() / 60.0)
    fresh = clamp(1.0 - age_min / max(config.max_chain_age_minutes, 1))
    components = {
        "direction_alignment": 1.0,
        "moneyness_suitability": money,
        "liquidity_quality": (liq_oi + liq_vol) / 2.0,
        "spread_quality": spread_s,
        "oi_quality": liq_oi,
        "volume_quality": liq_vol,
        "iv_suitability": iv_s,
        "greek_suitability": greek_s,
        "time_to_expiry": tte,
        "data_freshness": fresh,
    }
    total = round(sum(WEIGHTS[name] * components[name] for name in WEIGHTS), 4)
    why = (
        f"{kind} {option_type.value} {contract.strike:g} exp {contract.expiry.isoformat()} "
        f"score={total:.2f} spread={spread_pct if spread_pct is not None else 'n/a'} "
        f"oi={contract.open_interest} vol={contract.volume} "
        f"iv={'n/a' if contract.implied_volatility is None else f'{contract.implied_volatility:.2f}'} "
        f"delta={'n/a' if contract.delta is None else f'{contract.delta:.2f}'}"
    )
    _ = premium
    _ = abs_spread
    return ScoreBreakdown(
        version=SCORING_VERSION,
        total=total,
        components=components,
        weights=WEIGHTS,
        rationale=why,
    )


def rank_key(total: float, oi: int, volume: int, spread_pct: float | None, identity: tuple) -> tuple:
    """Ascending sort key: score DESC, OI DESC, volume DESC, spread ASC, identity ASC."""
    spread_asc = float("inf") if spread_pct is None else spread_pct
    return (-total, -oi, -volume, spread_asc, identity)
