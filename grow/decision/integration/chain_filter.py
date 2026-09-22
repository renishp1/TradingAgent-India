"""Campaign option-chain intelligence — sole candidate filter for the 4C path.

Agents may propose direction/strategy/instrument. Only contracts that survive
this deterministic chain filter may become paper TradeCandidates. Nothing is
fabricated; missing chain data fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from grow.clock import IST
from grow.config import GrowConfig, OptionsConfig, load_config
from grow.decision.integration.contract import StrategyCandidate
from grow.live_data.catalog import (
    UNKNOWN_EXPIRY_CLASS as CATALOG_UNKNOWN_EXPIRY,
)
from grow.market_data.normalized.models import AgentMarketSnapshot, DataQualityStatus, OptionQuoteView
from grow.options.models import ExpiryClass, OptionExpiry, OptionType
from grow.options.select import allowed_type, atm_strike, choose_expiry, strike_step, strike_window
from grow.options.validate import moneyness as classify_moneyness


MISSING_OPTION_CHAIN = "MISSING_OPTION_CHAIN"
CHAIN_UNIVERSE_EMPTY = "CHAIN_UNIVERSE_EMPTY"
CHAIN_FILTER_REJECTED = "CHAIN_FILTER_REJECTED"
CHAIN_FILTER_VERSION = "campaign.chain_filter.v1"
# Deterministic reject reason when expiry class is missing/UNKNOWN/invalid.
# Never fabricate WEEKLY — fail closed before choose_expiry.
UNKNOWN_EXPIRY_CLASS = "UNKNOWN_EXPIRY_CLASS"


def _contract_id(row: OptionQuoteView) -> str:
    return f"{row.underlying}-{row.expiry.isoformat()}-{int(row.strike)}-{row.option_type}"


@dataclass(frozen=True)
class ChainFilterResult:
    """Eligible campaign instruments derived from the shared agent snapshot."""

    eligible_instruments: tuple[str, ...]
    eligible_quotes: tuple[OptionQuoteView, ...]
    reason_codes: tuple[str, ...]
    diagnostics: Mapping[str, Any]
    selected_expiry: str | None = None
    atm: float | None = None

    @property
    def ok(self) -> bool:
        return bool(self.eligible_instruments) and not self.reason_codes


def filter_campaign_chain(
    snapshot: AgentMarketSnapshot,
    *,
    underlying: str,
    direction: str,
    config: GrowConfig | OptionsConfig | None = None,
) -> ChainFilterResult:
    """Build the sole allowlist of option instruments for the campaign path."""
    options_cfg = _options_config(config)
    under = underlying.strip().upper()
    wanted = allowed_type(direction)
    if wanted is None:
        return ChainFilterResult(
            eligible_instruments=(),
            eligible_quotes=(),
            reason_codes=(f"INVALID_DIRECTION:{direction}",),
            diagnostics={"filter_version": CHAIN_FILTER_VERSION},
        )
    if not options_cfg.enabled:
        return ChainFilterResult(
            eligible_instruments=(),
            eligible_quotes=(),
            reason_codes=("OPTIONS_DISABLED",),
            diagnostics={"filter_version": CHAIN_FILTER_VERSION},
        )

    as_of = snapshot.decision_timestamp.astimezone(IST)
    quotes = tuple(
        row
        for row in snapshot.option_contracts
        if row.underlying.upper() == under and row.quality is DataQualityStatus.OK
    )
    if not quotes:
        return ChainFilterResult(
            eligible_instruments=(),
            eligible_quotes=(),
            reason_codes=(MISSING_OPTION_CHAIN,),
            diagnostics={"filter_version": CHAIN_FILTER_VERSION, "underlying": under},
        )

    spot = _spot(snapshot, under)
    if spot is None or spot <= 0:
        return ChainFilterResult(
            eligible_instruments=(),
            eligible_quotes=(),
            reason_codes=("MISSING_SPOT",),
            diagnostics={"filter_version": CHAIN_FILTER_VERSION, "underlying": under},
        )

    expiries_or_reason = _expiries(quotes)
    if isinstance(expiries_or_reason, str):
        return ChainFilterResult(
            eligible_instruments=(),
            eligible_quotes=(),
            reason_codes=(expiries_or_reason,),
            diagnostics={
                "filter_version": CHAIN_FILTER_VERSION,
                "underlying": under,
                "expiry_class_policy": "fail_closed",
            },
            selected_expiry=None,
        )
    expiries = expiries_or_reason
    # Belt-and-suspenders: never hand OptionExpiry with missing/invalid klass to choose_expiry.
    guarded = _require_tradable_expiries(expiries)
    if isinstance(guarded, str):
        return ChainFilterResult(
            eligible_instruments=(),
            eligible_quotes=(),
            reason_codes=(guarded,),
            diagnostics={
                "filter_version": CHAIN_FILTER_VERSION,
                "underlying": under,
                "expiry_class_policy": "fail_closed",
            },
            selected_expiry=None,
        )
    expiries = guarded
    # Minimal chain shape for choose_expiry (no fabricate).
    from grow.options.models import OptionChainSnapshot

    chain = OptionChainSnapshot(
        snapshot_id=snapshot.snapshot_id,
        underlying=under,
        as_of=as_of,
        spot=spot,
        expiries=expiries,
        contracts=(),
        source_id=snapshot.provider or "agent_snapshot",
        is_fixture=bool(snapshot.is_fixture),
        provider_metadata={"campaign_chain_filter": CHAIN_FILTER_VERSION},
    )
    chosen, expiry_why = choose_expiry(chain, as_of, options_cfg)
    if chosen is None:
        return ChainFilterResult(
            eligible_instruments=(),
            eligible_quotes=(),
            reason_codes=(expiry_why,),
            diagnostics={"filter_version": CHAIN_FILTER_VERSION, "underlying": under},
            selected_expiry=None,
        )

    typed = tuple(
        row
        for row in quotes
        if row.expiry == chosen.day and row.option_type == wanted.value
    )
    strikes = tuple(row.strike for row in typed)
    uniq = tuple(sorted(set(strikes)))
    if not uniq:
        return ChainFilterResult(
            eligible_instruments=(),
            eligible_quotes=(),
            reason_codes=("NO_STRIKE_GRID",),
            diagnostics={
                "filter_version": CHAIN_FILTER_VERSION,
                "expiry": chosen.day.isoformat(),
                "option_type": wanted.value,
            },
        )
    if len(uniq) == 1:
        # Sparse live/fixture packets may publish a single ATM contract.
        window = uniq
        atm = uniq[0]
    else:
        step = strike_step(strikes)
        if step is None:
            return ChainFilterResult(
                eligible_instruments=(),
                eligible_quotes=(),
                reason_codes=("NO_STRIKE_GRID",),
                diagnostics={
                    "filter_version": CHAIN_FILTER_VERSION,
                    "expiry": chosen.day.isoformat(),
                    "option_type": wanted.value,
                },
            )
        window = strike_window(spot, strikes, options_cfg.max_distance_from_atm)
        atm = atm_strike(spot, window)
    if atm is None or not window:
        return ChainFilterResult(
            eligible_instruments=(),
            eligible_quotes=(),
            reason_codes=("NO_ATM_WINDOW",),
            diagnostics={"filter_version": CHAIN_FILTER_VERSION, "spot": spot},
        )

    kept: list[OptionQuoteView] = []
    rejected: list[str] = []
    for row in typed:
        reason = _quote_reject_reason(row, spot=spot, atm=atm, window=window, config=options_cfg, wanted=wanted)
        if reason is not None:
            rejected.append(f"{_instrument_names(row)[0]}:{reason}")
            continue
        kept.append(row)

    if not kept:
        return ChainFilterResult(
            eligible_instruments=(),
            eligible_quotes=(),
            reason_codes=(CHAIN_UNIVERSE_EMPTY,),
            diagnostics={
                "filter_version": CHAIN_FILTER_VERSION,
                "underlying": under,
                "expiry": chosen.day.isoformat(),
                "option_type": wanted.value,
                "atm": atm,
                "window": list(window),
                "rejected": rejected[:32],
                "expiry_why": expiry_why,
            },
            selected_expiry=chosen.day.isoformat(),
            atm=atm,
        )

    instruments: list[str] = []
    for row in kept:
        for name in _instrument_names(row):
            if name not in instruments:
                instruments.append(name)
    return ChainFilterResult(
        eligible_instruments=tuple(instruments),
        eligible_quotes=tuple(kept),
        reason_codes=(),
        diagnostics={
            "filter_version": CHAIN_FILTER_VERSION,
            "underlying": under,
            "expiry": chosen.day.isoformat(),
            "option_type": wanted.value,
            "atm": atm,
            "window": list(window),
            "eligible_count": len(kept),
            "rejected_count": len(rejected),
            "expiry_why": expiry_why,
        },
        selected_expiry=chosen.day.isoformat(),
        atm=atm,
    )


def allow_campaign_candidate(
    snapshot: AgentMarketSnapshot,
    candidate: StrategyCandidate,
    *,
    config: GrowConfig | OptionsConfig | None = None,
) -> tuple[str | None, ChainFilterResult]:
    """Return (reject_code, filter_result). reject_code is None when allowed."""
    filtered = filter_campaign_chain(
        snapshot,
        underlying=candidate.underlying,
        direction=candidate.direction,
        config=config,
    )
    if filtered.reason_codes:
        return filtered.reason_codes[0], filtered
    want = candidate.instrument.strip()
    want_upper = want.upper()
    for row in filtered.eligible_quotes:
        names = _instrument_names(row)
        if want in names or want_upper in {name.upper() for name in names}:
            return None, filtered
    # Lazy import avoids paper.__init__ → engine → policy circular import.
    from grow.paper.quotes import match_contract

    quote = match_contract(snapshot, candidate.instrument, candidate.underlying)
    if quote is not None and any(_same_contract(quote, row) for row in filtered.eligible_quotes):
        return None, filtered
    return CHAIN_FILTER_REJECTED, filtered


def _same_contract(left: OptionQuoteView, right: OptionQuoteView) -> bool:
    return (
        left.underlying == right.underlying
        and left.expiry == right.expiry
        and left.strike == right.strike
        and left.option_type == right.option_type
    )


def _instrument_names(row: OptionQuoteView) -> tuple[str, ...]:
    return (
        _contract_id(row),
        row.provider_contract_id,
        f"{row.underlying}-{row.strike:g}-{row.option_type}",
    )


def _quote_reject_reason(
    row: OptionQuoteView,
    *,
    spot: float,
    atm: float,
    window: tuple[float, ...],
    config: OptionsConfig,
    wanted: OptionType,
) -> str | None:
    if row.option_type != wanted.value:
        return "TYPE_MISMATCH"
    if row.strike not in window:
        return "OUTSIDE_ATM_WINDOW"
    if row.ltp is None or row.ltp <= 0:
        return "NO_PREMIUM"
    if row.bid is None or row.ask is None:
        return "MISSING_BID_ASK"
    if row.ask < row.bid:
        return "CROSSED_QUOTE"
    mid = (row.bid + row.ask) / 2.0
    if mid <= 0:
        return "NO_PREMIUM"
    spread_pct = (row.ask - row.bid) / mid
    if spread_pct > config.max_spread_pct + 1e-12:
        return "WIDE_SPREAD"
    kind = classify_moneyness(wanted, spot, row.strike, atm)
    if kind not in config.allowed_moneyness:
        return f"MONEYNESS:{kind}"
    return None


def _spot(snapshot: AgentMarketSnapshot, underlying: str) -> float | None:
    view = snapshot.underlyings.get(underlying) or snapshot.underlyings.get(underlying.upper())
    if view is None:
        for key, row in snapshot.underlyings.items():
            if key.upper() == underlying.upper():
                view = row
                break
    if view is None:
        return None
    if view.spot is not None and view.spot > 0:
        return float(view.spot)
    if view.ltp is not None and view.ltp > 0:
        return float(view.ltp)
    return None


def _expiries(quotes: tuple[OptionQuoteView, ...]) -> tuple[OptionExpiry, ...] | str:
    """Build expiry markers. Fail closed on missing/unknown/invalid class — never invent WEEKLY.

    Never constructs ``OptionExpiry(day, None)``. Bad classification returns
    ``UNKNOWN_EXPIRY_CLASS`` so ``filter_campaign_chain`` never calls ``choose_expiry``.
    """
    seen: dict[date, ExpiryClass] = {}
    for row in quotes:
        klass = _expiry_class(row.expiry_class)
        if klass is None:
            return UNKNOWN_EXPIRY_CLASS
        if klass not in (ExpiryClass.WEEKLY, ExpiryClass.MONTHLY):
            return UNKNOWN_EXPIRY_CLASS
        prior = seen.get(row.expiry)
        if prior is not None and prior is not klass:
            return UNKNOWN_EXPIRY_CLASS
        seen[row.expiry] = klass
    if not seen:
        return UNKNOWN_EXPIRY_CLASS
    built: list[OptionExpiry] = []
    for day, klass in sorted(seen.items(), key=lambda item: item[0]):
        # klass is ExpiryClass.WEEKLY|MONTHLY only — never None.
        built.append(OptionExpiry(day, klass))
    return tuple(built)


def _require_tradable_expiries(expiries: tuple[OptionExpiry, ...]) -> tuple[OptionExpiry, ...] | str:
    """Reject any expiry marker that is missing or not WEEKLY/MONTHLY before choose_expiry."""
    if not expiries:
        return UNKNOWN_EXPIRY_CLASS
    for item in expiries:
        if item.klass is None:
            return UNKNOWN_EXPIRY_CLASS
        if item.klass not in (ExpiryClass.WEEKLY, ExpiryClass.MONTHLY):
            return UNKNOWN_EXPIRY_CLASS
    return expiries


# Strict allowlist only. Missing keys (None/UNKNOWN/invalid) → None. Never default to WEEKLY.
_ALLOWED_EXPIRY_CLASS: Mapping[str, ExpiryClass] = {
    ExpiryClass.WEEKLY.value: ExpiryClass.WEEKLY,
    ExpiryClass.MONTHLY.value: ExpiryClass.MONTHLY,
}


def _expiry_class(raw: str | None) -> ExpiryClass | None:
    """Fail-closed expiry classification for the campaign chain filter.

    WEEKLY / weekly   → ExpiryClass.WEEKLY
    MONTHLY / monthly → ExpiryClass.MONTHLY
    None / UNKNOWN / any other string → None (caller rejects with UNKNOWN_EXPIRY_CLASS)
    """
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if not text:
        return None
    # Explicit reject of catalog UNKNOWN and this module's reject token.
    if text in {CATALOG_UNKNOWN_EXPIRY, UNKNOWN_EXPIRY_CLASS, "UNKNOWN"}:
        return None
    return _ALLOWED_EXPIRY_CLASS.get(text)


def _options_config(config: GrowConfig | OptionsConfig | None) -> OptionsConfig:
    if isinstance(config, OptionsConfig):
        return config
    if config is None:
        return load_config().options
    return config.options
