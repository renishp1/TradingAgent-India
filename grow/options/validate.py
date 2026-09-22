"""Option-chain integrity. Fail closed. Never fabricate missing fields."""

from __future__ import annotations

from datetime import datetime

from grow.clock import IST
from grow.config import OptionsConfig
from grow.options.models import FieldSource, OptionChainSnapshot, OptionContract, OptionType, RejectedContract

_VALID_SOURCE = frozenset({FieldSource.PROVIDER, FieldSource.COMPUTED})
_INDEX = frozenset({"NIFTY", "BANKNIFTY"})
_FORBIDDEN_UNDERLYING = frozenset({"RELIANCE", "TCS", "FUT", "FUTURES"})
_CHAIN_PROVIDERS = frozenset({"fixture", "historical", "paper_stream"})


def assert_ist(moment: datetime, field: str) -> None:
    if moment.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    key = getattr(moment.tzinfo, "key", None)
    if key != "Asia/Kolkata":
        raise ValueError(f"{field} must be Asia/Kolkata, got {moment.tzinfo!r}")


def premium(contract: OptionContract) -> float | None:
    if contract.bid is not None and contract.ask is not None:
        return round((contract.bid + contract.ask) / 2.0, 4)
    return contract.last_price


def spread(contract: OptionContract) -> tuple[float | None, float | None]:
    if contract.bid is None or contract.ask is None:
        return None, None
    abs_spread = contract.ask - contract.bid
    mid = (contract.bid + contract.ask) / 2.0
    pct = abs_spread / mid if mid > 0 else None
    return abs_spread, pct


def intrinsic(option_type: OptionType, spot: float, strike: float) -> float:
    if option_type is OptionType.CE:
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def moneyness(option_type: OptionType, spot: float, strike: float, atm: float) -> str:
    if strike == atm:
        return "ATM"
    itm = strike < spot if option_type is OptionType.CE else strike > spot
    return "ITM" if itm else "OTM"


def validate_chain(
    chain: OptionChainSnapshot,
    *,
    as_of: datetime,
    config: OptionsConfig,
) -> tuple[tuple[OptionContract, ...], tuple[RejectedContract, ...], tuple[str, ...]]:
    diagnostics: list[str] = []
    rejected: list[RejectedContract] = []
    kept: list[OptionContract] = []
    assert_ist(chain.as_of, "chain.as_of")
    assert_ist(as_of, "as_of")
    if config.provider == "paper_stream":
        from grow.history.universe import default_index_registry, is_forbidden_instrument

        if is_forbidden_instrument(chain.underlying) or not default_index_registry().allows(chain.underlying):
            return (), (RejectedContract(("*", "*", 0.0, "*"), f"UNSUPPORTED_UNDERLYING:{chain.underlying}"),), (
                f"UNSUPPORTED_UNDERLYING:{chain.underlying}",
            )
    elif chain.underlying not in _INDEX:
        return (), (RejectedContract(("*", "*", 0.0, "*"), f"UNSUPPORTED_UNDERLYING:{chain.underlying}"),), (
            f"UNSUPPORTED_UNDERLYING:{chain.underlying}",
        )
    if config.allow_live_chain or config.provider == "live" or "live" in chain.source_id.lower():
        return (), (), ("LIVE_CHAIN_FORBIDDEN",)
    if config.provider == "fixture" and chain.is_fixture is False:
        return (), (), ("LIVE_CHAIN_FORBIDDEN",)
    if config.provider == "paper_stream" and chain.is_fixture is True:
        return (), (), ("FIXTURE_FALLBACK_FORBIDDEN",)
    if config.provider not in _CHAIN_PROVIDERS:
        return (), (), ("LIVE_CHAIN_FORBIDDEN",)
    age = (as_of - chain.as_of).total_seconds() / 60.0
    if chain.as_of > as_of:
        return (), (), ("CHAIN_FROM_FUTURE",)
    if config.safety_reject_stale and age > config.max_chain_age_minutes:
        return (), (), (f"STALE_CHAIN:{age:.1f}m>{config.max_chain_age_minutes}m",)
    seen: set[tuple[str, str, float, str]] = set()
    for contract in chain.contracts:
        reason = _contract_reason(contract, chain=chain, as_of=as_of, config=config)
        ident = contract.identity()
        if ident in seen:
            rejected.append(RejectedContract(ident, "DUPLICATE_CONTRACT"))
            continue
        seen.add(ident)
        if reason is not None:
            rejected.append(RejectedContract(ident, reason))
            continue
        kept.append(contract)
    diagnostics.append(f"kept={len(kept)} rejected={len(rejected)}")
    return tuple(kept), tuple(rejected), tuple(diagnostics)


def _contract_reason(
    contract: OptionContract,
    *,
    chain: OptionChainSnapshot,
    as_of: datetime,
    config: OptionsConfig,
) -> str | None:
    if contract.underlying != chain.underlying:
        return "UNDERLYING_MISMATCH"
    if config.provider == "paper_stream":
        from grow.history.universe import default_index_registry, is_forbidden_instrument

        if is_forbidden_instrument(contract.underlying) or not default_index_registry().allows(contract.underlying):
            return "UNSUPPORTED_UNDERLYING"
    elif contract.underlying not in _INDEX:
        return "UNSUPPORTED_UNDERLYING"
    if contract.underlying in _FORBIDDEN_UNDERLYING:
        return "STOCK_OR_FUTURE"
    try:
        assert_ist(contract.timestamp, "contract.timestamp")
    except ValueError:
        return "TIMEZONE"
    if contract.timestamp > as_of:
        return "FUTURE_QUOTE"
    quote_age_min = (as_of - contract.timestamp).total_seconds() / 60.0
    if quote_age_min > config.max_quote_age_minutes:
        return "STALE_QUOTE"
    if contract.expiry < as_of.astimezone(IST).date():
        return "EXPIRED"
    if contract.strike <= 0:
        return "INVALID_STRIKE"
    if contract.volume < 0:
        return "NEGATIVE_VOLUME"
    if contract.open_interest < 0:
        return "NEGATIVE_OI"
    if contract.bid is not None and contract.bid < 0:
        return "NEGATIVE_BID"
    if contract.ask is not None and contract.ask < 0:
        return "NEGATIVE_ASK"
    if config.safety_reject_invalid_quotes and contract.bid is not None and contract.ask is not None:
        if contract.ask < contract.bid:
            return "CROSSED_QUOTE"
    if contract.option_type not in (OptionType.CE, OptionType.PE):
        return "INVALID_TYPE"
    if contract.implied_volatility is not None:
        if contract.iv_source not in _VALID_SOURCE:
            return "IV_SOURCE_MISSING"
        if not (config.iv_min <= contract.implied_volatility <= config.iv_max):
            return "IV_OUT_OF_BOUNDS"
    greeks = (contract.delta, contract.gamma, contract.theta, contract.vega)
    if any(value is not None for value in greeks) and contract.greek_source not in _VALID_SOURCE:
        return "GREEK_SOURCE_MISSING"
    px = premium(contract)
    if px is None or px <= 0:
        return "NO_PREMIUM"
    return None
