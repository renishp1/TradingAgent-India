"""Campaign-path option scoring — reuses IndexOptionsEngine DTE buckets.

Ranks allowlisted ``OptionQuoteView`` rows for campaign specialist selection.
Calendar time-to-expiry is scored (same buckets as ``options.score.v1``).
Theta is recorded when present but does not change the score (documented).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from grow.clock import IST
from grow.config import GrowConfig, OptionsConfig, load_config
from grow.market_data.normalized.models import OptionQuoteView
from grow.options.models import ExpiryClass, FieldSource, OptionContract, OptionType
from grow.options.score import SCORING_VERSION, rank_key, score_contract
from grow.options.select import session_day
from grow.options.validate import spread as quote_spread


def options_config(config: GrowConfig | OptionsConfig | None) -> OptionsConfig:
    if isinstance(config, OptionsConfig):
        return config
    if config is None:
        return load_config().options
    return config.options


def _expiry_class(raw: str | None) -> ExpiryClass:
    text = str(raw or "").strip().upper()
    if text == ExpiryClass.MONTHLY.value:
        return ExpiryClass.MONTHLY
    return ExpiryClass.WEEKLY


def quote_to_contract(row: OptionQuoteView) -> OptionContract:
    """Map a normalized quote into the scoring contract shape."""
    has_greeks = any(v is not None for v in (row.delta, row.gamma, row.theta, row.vega))
    return OptionContract(
        underlying=row.underlying,
        expiry=row.expiry,
        expiry_class=_expiry_class(row.expiry_class),
        strike=float(row.strike),
        option_type=OptionType.CE if row.option_type.upper() == "CE" else OptionType.PE,
        bid=row.bid,
        ask=row.ask,
        last_price=row.ltp,
        volume=int(row.volume or 0),
        open_interest=int(row.open_interest or 0),
        previous_open_interest=row.previous_open_interest,
        implied_volatility=row.implied_volatility,
        delta=row.delta,
        gamma=row.gamma,
        theta=row.theta,
        vega=row.vega,
        timestamp=row.quote_timestamp,
        provider_contract_id=row.provider_contract_id,
        iv_source=FieldSource.PROVIDER if row.implied_volatility is not None else FieldSource.UNAVAILABLE,
        greek_source=FieldSource.PROVIDER if has_greeks else FieldSource.UNAVAILABLE,
    )


def score_campaign_quote(
    row: OptionQuoteView,
    *,
    spot: float,
    atm: float,
    as_of: datetime,
    option_type: OptionType,
    config: OptionsConfig,
) -> tuple[float, dict[str, Any]]:
    """Return (total_score, diagnostics). Includes DTE bucket; theta is informational only."""
    contract = quote_to_contract(row)
    breakdown = score_contract(
        contract,
        spot=spot,
        atm=atm,
        as_of=as_of,
        chain_as_of=row.quote_timestamp.astimezone(IST),
        option_type=option_type,
        config=config,
    )
    days = (row.expiry - session_day(as_of)).days
    diagnostics = {
        "score_version": SCORING_VERSION,
        "total": breakdown.total,
        "components": dict(breakdown.components),
        "dte_days": days,
        "time_to_expiry": breakdown.components.get("time_to_expiry"),
        "theta": row.theta,
        "theta_in_score": False,
        "rationale": breakdown.rationale,
    }
    return breakdown.total, diagnostics


def rank_campaign_quotes(
    quotes: tuple[OptionQuoteView, ...],
    *,
    spot: float,
    atm: float,
    as_of: datetime,
    option_type: OptionType,
    config: OptionsConfig,
) -> tuple[OptionQuoteView, ...] | None:
    """Best-first ordering. Empty input → None."""
    if not quotes:
        return None
    scored: list[tuple[tuple, OptionQuoteView]] = []
    for row in quotes:
        total, _diag = score_campaign_quote(
            row,
            spot=spot,
            atm=atm,
            as_of=as_of,
            option_type=option_type,
            config=config,
        )
        _, spread_pct = quote_spread(quote_to_contract(row))
        key = rank_key(
            total,
            int(row.open_interest or 0),
            int(row.volume or 0),
            spread_pct,
            (row.expiry.isoformat(), row.strike, row.option_type, row.provider_contract_id),
        )
        scored.append((key, row))
    scored.sort(key=lambda item: item[0])
    return tuple(row for _, row in scored)


__all__ = [
    "options_config",
    "quote_to_contract",
    "rank_campaign_quotes",
    "score_campaign_quote",
]
