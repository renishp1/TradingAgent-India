"""Index options research engine. BUY CE / BUY PE candidates only. No execution."""

from __future__ import annotations

from grow.config import GrowConfig, OptionsConfig, load_config
from grow.data.schema import MarketSnapshot
from grow.errors import GrowConfigError
from grow.options.models import (
    DecisionStatus,
    OptionCandidate,
    OptionChainSnapshot,
    OptionType,
    OptionsDecision,
    RejectedContract,
    candidate_id,
)
from grow.options.score import SCORING_VERSION, rank_key, score_contract
from grow.options.select import (
    allowed_type,
    atm_strike,
    choose_expiry,
    filter_universe,
    strike_step,
    strike_window,
)
from grow.options.validate import (
    assert_ist,
    intrinsic,
    moneyness,
    premium,
    spread,
    validate_chain,
)
from grow.strategies.signal import StrategySignal

SELECTION_VERSION = "options.select.v1"


class IndexOptionsEngine:
    def __init__(self, config: GrowConfig | None = None) -> None:
        self.config = config or load_config()
        if self.config.options.allow_live_chain or self.config.options.provider == "live":
            raise GrowConfigError("Live option chains are not attached.")
        if self.config.options.provider not in {"fixture", "historical", "paper_stream"}:
            raise GrowConfigError("2C only evaluates fixture, historical, or paper_stream chains.")

    def evaluate(
        self,
        signal: StrategySignal,
        underlying_snapshot: MarketSnapshot,
        option_chain: OptionChainSnapshot,
        config: GrowConfig | None = None,
        registry=None,
    ) -> OptionsDecision:
        cfg = (config or self.config).options
        as_of = underlying_snapshot.as_of
        sid = signal.signal_id or signal.snapshot_id
        empty = lambda reasons, rejected=(): OptionsDecision(  # noqa: E731
            status=DecisionStatus.NO_TRADE,
            candidate=None,
            rejected=rejected,
            diagnostics=tuple(reasons),
            as_of=as_of,
            strategy_signal_id=sid,
            option_chain_snapshot_id=option_chain.snapshot_id,
            underlying_snapshot_id=underlying_snapshot.snapshot_id,
        )
        if not cfg.enabled:
            return empty(("OPTIONS_DISABLED",))
        try:
            assert_ist(as_of, "snapshot.as_of")
            assert_ist(signal.as_of, "signal.as_of")
        except ValueError as exc:
            return empty((str(exc),))
        if signal.direction in {"LONG", "SHORT", "SELL", "BUY", "OPTION_SELL"}:
            return empty((f"EXECUTION_LANGUAGE:{signal.direction}",))
        from grow.history.universe import apply_index_policy, default_index_registry, is_forbidden_instrument

        overlay = registry or default_index_registry()
        ticker = signal.symbol.ticker
        if is_forbidden_instrument(ticker) or not overlay.allows(ticker, as_of.date()):
            return empty((f"UNSUPPORTED_UNDERLYING:{ticker}",))
        if option_chain.underlying != ticker:
            return empty(("UNDERLYING_CHAIN_MISMATCH",))
        if underlying_snapshot.symbol.ticker != signal.symbol.ticker:
            return empty(("UNDERLYING_SNAPSHOT_MISMATCH",))
        if signal.snapshot_id != underlying_snapshot.snapshot_id:
            return empty(("SNAPSHOT_ID_MISMATCH",))
        if abs((signal.as_of - underlying_snapshot.as_of).total_seconds()) > 1:
            return empty(("SIGNAL_ASOF_MISMATCH",))
        if abs(option_chain.spot - underlying_snapshot.last_price) > 0.01:
            return empty(("CHAIN_SPOT_MISMATCH",))
        if abs((option_chain.as_of - as_of).total_seconds()) > cfg.max_chain_age_minutes * 60:
            if option_chain.as_of > as_of:
                return empty(("CHAIN_FROM_FUTURE",))
            if cfg.safety_reject_stale:
                return empty(("STALE_CHAIN",))
        wanted = allowed_type(signal.direction)
        if wanted is None:
            return empty((f"INVALID_DIRECTION:{signal.direction}",))
        kept, rejected, notes = validate_chain(option_chain, as_of=as_of, config=cfg)
        if not kept and any(n.startswith("STALE") or n in {"CHAIN_FROM_FUTURE", "LIVE_CHAIN_FORBIDDEN"} for n in notes):
            return empty(notes, rejected)
        policy = overlay.policy(ticker, as_of.date())
        if policy is not None:
            try:
                cfg = apply_index_policy(cfg, policy)
            except GrowConfigError as exc:
                return empty((str(exc),))
        expiry, expiry_why = choose_expiry(
            option_chain,
            as_of,
            cfg,
            policy_profile=None if policy is None else policy.expiry_policy_profile,
        )
        if expiry is None:
            return empty((expiry_why, *notes), rejected)
        spot = underlying_snapshot.last_price
        strikes = tuple(c.strike for c in kept if c.expiry == expiry.day)
        if strike_step(strikes) is None:
            return empty(("NO_STRIKE_STEP", *notes), rejected)
        window = strike_window(spot, strikes, cfg.max_distance_from_atm)
        rejected_list = list(rejected)
        for contract in kept:
            if contract.option_type is not wanted and contract.expiry == expiry:
                rejected_list.append(RejectedContract(contract.identity(), f"DIRECTION_GATE:{wanted.value}"))
        if not window:
            return empty(("NO_PERMITTED_STRIKE", *notes), tuple(rejected_list))
        universe = filter_universe(
            kept,
            expiry=expiry,
            option_type=wanted,
            spot=spot,
            window=window,
            allowed_moneyness=cfg.allowed_moneyness,
        )
        if not universe:
            return empty(("NO_PERMITTED_STRIKE", expiry_why, *notes), tuple(rejected_list))
        scored: list[tuple[tuple, OptionCandidate]] = []
        atm = atm_strike(spot, window)
        if atm is None:
            return empty(("ATM_UNAVAILABLE",), tuple(rejected_list))
        for contract in universe:
            reason = _liquidity_reason(contract, cfg)
            if reason:
                rejected_list.append(RejectedContract(contract.identity(), reason))
                continue
            px = premium(contract)
            assert px is not None
            inn = round(intrinsic(wanted, spot, contract.strike), 4)
            ext = round(px - inn, 4)
            if ext < -0.05:
                rejected_list.append(RejectedContract(contract.identity(), "NEGATIVE_EXTRINSIC"))
                continue
            abs_spread, spread_pct = spread(contract)
            breakdown = score_contract(
                contract,
                spot=spot,
                atm=atm,
                as_of=as_of,
                chain_as_of=option_chain.as_of,
                option_type=wanted,
                config=cfg,
            )
            cand = OptionCandidate(
                candidate_id=candidate_id(
                    underlying=contract.underlying,
                    expiry=contract.expiry.isoformat(),
                    strike=contract.strike,
                    option_type=contract.option_type.value,
                    chain_id=option_chain.snapshot_id,
                    signal_id=sid,
                    selection_version=SELECTION_VERSION,
                ),
                underlying=contract.underlying,
                direction=signal.direction,
                option_type=contract.option_type.value,
                intent="BUY",
                expiry=contract.expiry,
                strike=contract.strike,
                contract_symbol=contract.provider_contract_id,
                spot_price=spot,
                premium_reference=px,
                bid=contract.bid,
                ask=contract.ask,
                spread=abs_spread,
                spread_pct=spread_pct,
                volume=contract.volume,
                open_interest=contract.open_interest,
                implied_volatility=contract.implied_volatility,
                delta=contract.delta,
                gamma=contract.gamma,
                theta=contract.theta,
                vega=contract.vega,
                intrinsic_value=inn,
                extrinsic_value=ext,
                moneyness=moneyness(wanted, spot, contract.strike, atm),
                score=breakdown,
                as_of=as_of,
                underlying_snapshot_id=underlying_snapshot.snapshot_id,
                option_chain_snapshot_id=option_chain.snapshot_id,
                strategy_signal_id=sid,
                strategy_version=signal.strategy_version,
                selection_version=SELECTION_VERSION,
                reasons=(expiry_why, breakdown.rationale),
            )
            key = rank_key(
                breakdown.total,
                contract.open_interest,
                contract.volume,
                spread_pct,
                contract.identity(),
            )
            scored.append((key, cand))
        if not scored:
            return empty(("NO_LIQUID_CONTRACT", expiry_why, *notes), tuple(rejected_list))
        scored.sort(key=lambda row: row[0])
        winner = scored[0][1]
        return OptionsDecision(
            status=DecisionStatus.CANDIDATE,
            candidate=winner,
            rejected=tuple(rejected_list),
            diagnostics=(expiry_why, f"scored={len(scored)}", f"winner={winner.candidate_id}", *notes),
            as_of=as_of,
            strategy_signal_id=sid,
            option_chain_snapshot_id=option_chain.snapshot_id,
            underlying_snapshot_id=underlying_snapshot.snapshot_id,
        )


def _liquidity_reason(contract, cfg: OptionsConfig) -> str | None:
    if contract.volume < cfg.min_volume:
        return "LOW_VOLUME"
    if contract.open_interest < cfg.min_open_interest:
        return "LOW_OI"
    _, spread_pct = spread(contract)
    if spread_pct is None:
        return "NO_SPREAD"
    if spread_pct > cfg.max_spread_pct:
        return "WIDE_SPREAD"
    return None
