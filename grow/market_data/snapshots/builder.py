"""Build provider-neutral agent snapshots from live or fixture sources."""

from __future__ import annotations

from datetime import datetime

from grow.clock import IST
from grow.data.schema import MarketSnapshot, Timeframe
from grow.errors import GrowConfigError
from grow.live_data.models import LiveSnapshot
from grow.market_data.normalized.models import (
    SCHEMA,
    AgentMarketSnapshot,
    DataQualityStatus,
    OptionQuoteView,
    UnderlyingQuoteView,
    snapshot_digest,
)
from grow.options.models import OptionChainSnapshot, OptionContract


class SnapshotBuildError(GrowConfigError):
    """Snapshot could not be built or failed the quality gate."""


def gate_snapshot_quality(snapshot: AgentMarketSnapshot) -> DataQualityStatus:
    """Fail closed when required point-in-time data is missing or stale."""
    if snapshot.data_quality in {
        DataQualityStatus.INSUFFICIENT,
        DataQualityStatus.REJECTED,
        DataQualityStatus.STALE,
    }:
        return snapshot.data_quality
    if not snapshot.underlyings:
        return DataQualityStatus.INSUFFICIENT
    if any(u.ltp is None and u.spot is None for u in snapshot.underlyings.values()):
        return DataQualityStatus.INSUFFICIENT
    return snapshot.data_quality


def build_agent_snapshot(
    live: LiveSnapshot,
    *,
    decision_timestamp: datetime,
    exchange: str = "NSE",
    max_quote_age_seconds: float | None = None,
) -> AgentMarketSnapshot:
    """Map a validated LiveSnapshot into the common agent snapshot contract."""
    decision_ts = decision_timestamp.astimezone(IST)
    if live.event_time.astimezone(IST) > decision_ts:
        raise SnapshotBuildError("FUTURE_SNAPSHOT")

    underlyings: dict[str, UnderlyingQuoteView] = {}
    for symbol, market in live.market.items():
        underlyings[symbol] = _underlying_from_market(market, decision_ts, exchange)

    options: list[OptionQuoteView] = []
    notes: list[str] = list(live.diagnostics)
    quality = DataQualityStatus.OK if live.freshness_ok else DataQualityStatus.STALE
    if not live.freshness_ok:
        notes.append("LIVE_FRESHNESS_FAIL")

    for underlying, chain in live.chains.items():
        for contract in chain.contracts:
            age = (decision_ts - contract.timestamp.astimezone(IST)).total_seconds()
            contract_quality = DataQualityStatus.OK
            if max_quote_age_seconds is not None and age > max_quote_age_seconds:
                contract_quality = DataQualityStatus.STALE
                quality = DataQualityStatus.STALE
                notes.append(f"STALE_OPTION:{contract.provider_contract_id}")
            options.append(_option_from_contract(contract, age, contract_quality))

    if not underlyings:
        quality = DataQualityStatus.INSUFFICIENT
        notes.append("NO_UNDERLYINGS")

    source_ids = {
        "live": live.snapshot_id,
        **{f"market:{sym}": snap.snapshot_id for sym, snap in live.market.items()},
        **{f"chain:{sym}": chain.snapshot_id for sym, chain in live.chains.items()},
    }
    version = snapshot_digest(
        {
            "decision": decision_ts.isoformat(),
            "live": live.snapshot_id,
            "provider": live.provider_id,
            "sequence": live.sequence,
        }
    )
    return AgentMarketSnapshot(
        snapshot_id=f"agent-{version}",
        version=version,
        schema=SCHEMA,
        provider=live.provider_id,
        exchange=exchange,
        session_timestamp=live.event_time.astimezone(IST),
        decision_timestamp=decision_ts,
        session_date=live.session_date,
        underlyings=underlyings,
        option_contracts=tuple(options),
        data_quality=quality,
        quality_notes=tuple(dict.fromkeys(notes)),
        source_snapshot_ids=source_ids,
        diagnostics={
            "sequence": live.sequence,
            "adapter_version": live.adapter_version,
            "freshness_ok": live.freshness_ok,
        },
    )


def build_fixture_snapshot(
    *,
    underlying: str,
    as_of: datetime,
    spot: float,
    option_contracts: tuple[OptionQuoteView, ...] = (),
    provider: str = "grow.fixture.agent.v1",
    exchange: str = "NSE",
    quality: DataQualityStatus = DataQualityStatus.OK,
    notes: tuple[str, ...] = (),
    market: MarketSnapshot | None = None,
    chain: OptionChainSnapshot | None = None,
) -> AgentMarketSnapshot:
    """Deterministic fixture snapshot for unit tests and offline cycles."""
    decision_ts = as_of.astimezone(IST)
    underlyings = {
        underlying: UnderlyingQuoteView(
            underlying=underlying,
            exchange=exchange,
            spot=spot,
            ltp=spot,
            open=spot,
            high=spot,
            low=spot,
            close=spot,
            volume=0,
            quote_timestamp=decision_ts,
            quote_age_seconds=0.0,
        )
    }
    source_ids: dict[str, str] = {"fixture": "fixture"}
    if market is not None:
        underlyings[underlying] = _underlying_from_market(market, decision_ts, exchange)
        source_ids["market"] = market.snapshot_id
    if chain is not None:
        option_contracts = tuple(
            _option_from_contract(
                contract,
                (decision_ts - contract.timestamp.astimezone(IST)).total_seconds(),
                DataQualityStatus.OK,
            )
            for contract in chain.contracts
        )
        source_ids["chain"] = chain.snapshot_id
    version = snapshot_digest(
        {
            "as_of": decision_ts.isoformat(),
            "underlying": underlying,
            "spot": spot,
            "options": [c.to_dict() for c in option_contracts],
            "quality": quality.value,
        }
    )
    return AgentMarketSnapshot(
        snapshot_id=f"agent-{version}",
        version=version,
        schema=SCHEMA,
        provider=provider,
        exchange=exchange,
        session_timestamp=decision_ts,
        decision_timestamp=decision_ts,
        session_date=decision_ts.date(),
        underlyings=underlyings,
        option_contracts=option_contracts,
        data_quality=quality,
        quality_notes=notes,
        source_snapshot_ids=source_ids,
        diagnostics={"fixture": True},
    )


def _underlying_from_market(
    market: MarketSnapshot,
    decision_ts: datetime,
    exchange: str,
) -> UnderlyingQuoteView:
    open_px = high = low = close = None
    volume = None
    d1 = market.series.get(Timeframe.D1)
    if d1 is not None and d1.bars:
        bar = d1.bars[-1]
        open_px, high, low, close = bar.open, bar.high, bar.low, bar.close
        volume = bar.volume
    age = None
    if market.as_of is not None:
        age = (decision_ts - market.as_of.astimezone(IST)).total_seconds()
    return UnderlyingQuoteView(
        underlying=market.symbol.ticker,
        exchange=exchange or market.symbol.exchange,
        spot=market.last_price,
        ltp=market.last_price,
        open=open_px,
        high=high,
        low=low,
        close=close,
        volume=volume,
        quote_timestamp=market.as_of,
        quote_age_seconds=age,
    )


def _option_from_contract(
    contract: OptionContract,
    age: float,
    quality: DataQualityStatus,
) -> OptionQuoteView:
    return OptionQuoteView(
        underlying=contract.underlying,
        expiry=contract.expiry,
        strike=float(contract.strike),
        option_type=contract.option_type.value,
        ltp=contract.last_price,
        bid=contract.bid,
        ask=contract.ask,
        open_interest=contract.open_interest,
        volume=contract.volume,
        quote_timestamp=contract.timestamp.astimezone(IST),
        quote_age_seconds=age,
        provider_contract_id=contract.provider_contract_id,
        quality=quality,
    )
