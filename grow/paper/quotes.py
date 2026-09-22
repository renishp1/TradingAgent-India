"""Map an agent snapshot onto the 3B live snapshot used for marks and exits."""

from __future__ import annotations

from typing import Any

from grow.clock import IST
from grow.live_data.models import SCHEMA, LiveSnapshot
from grow.market_data.normalized.models import AgentMarketSnapshot, DataQualityStatus, OptionQuoteView
from grow.options.models import ExpiryClass, OptionChainSnapshot, OptionContract, OptionExpiry, OptionType


def contract_id(quote: OptionQuoteView) -> str:
    return f"{quote.underlying}-{quote.expiry.isoformat()}-{int(quote.strike)}-{quote.option_type}"


def match_contract(snapshot: AgentMarketSnapshot, instrument: str, underlying: str | None) -> OptionQuoteView | None:
    """Resolve a 4C instrument to one option quote on this snapshot."""
    want = instrument.strip()
    upper = want.upper()
    found: list[OptionQuoteView] = []
    for row in snapshot.option_contracts:
        if underlying and row.underlying.upper() != underlying.upper():
            continue
        names = {
            row.provider_contract_id,
            f"{row.underlying}-{row.strike:g}-{row.option_type}",
            contract_id(row),
        }
        upper_names = {item.upper() for item in names}
        if want in names or upper in upper_names:
            found.append(row)
    if len(found) != 1:
        return None
    return found[0]


def quote_known_at(quote: OptionQuoteView, as_of) -> bool:
    return quote.quote_timestamp.astimezone(IST) <= as_of.astimezone(IST)


def source_is_fixture(snapshot: AgentMarketSnapshot) -> bool:
    """Paper execution is simulated; fixture vs live is about the market-data source."""
    if snapshot.is_fixture:
        return True
    diagnostics = dict(snapshot.diagnostics or {})
    if diagnostics.get("fixture") is True:
        return True
    if diagnostics.get("fixture") is False:
        return False
    provider = (snapshot.provider or "").lower()
    return "fixture" in provider


def live_snapshot_from_agent(snapshot: AgentMarketSnapshot, *, sequence: int) -> LiveSnapshot:
    """Quotes later than the snapshot decision time are omitted. Nothing is fabricated."""
    as_of = snapshot.decision_timestamp.astimezone(IST)
    is_fixture = source_is_fixture(snapshot)
    diagnostics = dict(snapshot.diagnostics or {})
    adapter_version = str(diagnostics.get("adapter_version") or snapshot.provider or "paper.execution.v1")
    grouped: dict[str, list[OptionContract]] = {}
    expiries: dict[str, list[OptionExpiry]] = {}
    lot_sizes: dict[str, int] = {}
    for quote in snapshot.option_contracts:
        if quote.quality is not DataQualityStatus.OK or not quote_known_at(quote, as_of):
            continue
        expiry_class = _expiry_class(quote.expiry_class)
        contract = OptionContract(
            underlying=quote.underlying,
            expiry=quote.expiry,
            expiry_class=expiry_class,
            strike=quote.strike,
            option_type=OptionType(quote.option_type),
            bid=quote.bid,
            ask=quote.ask,
            last_price=quote.ltp,
            volume=0 if quote.volume is None else int(quote.volume),
            open_interest=0 if quote.open_interest is None else int(quote.open_interest),
            previous_open_interest=quote.previous_open_interest,
            implied_volatility=quote.implied_volatility,
            delta=quote.delta,
            gamma=quote.gamma,
            theta=quote.theta,
            vega=quote.vega,
            timestamp=quote.quote_timestamp.astimezone(IST),
            provider_contract_id=quote.provider_contract_id,
        )
        grouped.setdefault(quote.underlying, []).append(contract)
        expiries.setdefault(quote.underlying, [])
        marker = OptionExpiry(quote.expiry, expiry_class)
        if marker not in expiries[quote.underlying]:
            expiries[quote.underlying].append(marker)
        if quote.lot_size is not None and quote.lot_size >= 1:
            lot_sizes[quote.provider_contract_id] = int(quote.lot_size)
            lot_sizes[contract_id(quote)] = int(quote.lot_size)
    chains = {
        underlying: OptionChainSnapshot(
            snapshot_id=snapshot.snapshot_id,
            underlying=underlying,
            as_of=as_of,
            spot=_spot(snapshot, underlying),
            expiries=tuple(expiries.get(underlying, ())),
            contracts=tuple(contracts),
            source_id=snapshot.provider,
            is_fixture=is_fixture,
            provider_metadata=_provider_metadata(snapshot, is_fixture=is_fixture),
        )
        for underlying, contracts in grouped.items()
    }
    return LiveSnapshot(
        snapshot_id=snapshot.snapshot_id,
        schema=SCHEMA,
        provider_id=snapshot.provider,
        adapter_version=adapter_version,
        sequence=sequence,
        event_time=as_of,
        received_time=as_of,
        session_date=snapshot.session_date,
        underlyings=tuple(snapshot.underlyings),
        market={},
        chains=chains,
        lot_sizes=lot_sizes,
        freshness_ok=snapshot.data_quality is DataQualityStatus.OK,
        diagnostics=snapshot.quality_notes,
    )


def _provider_metadata(snapshot: AgentMarketSnapshot, *, is_fixture: bool) -> dict[str, Any]:
    return {
        "paper_execution": True,
        "live_trading": False,
        "broker_order_path": False,
        "source_snapshot_id": snapshot.snapshot_id,
        "is_fixture": is_fixture,
        "provider": snapshot.provider,
        "quote_timestamp": snapshot.decision_timestamp.astimezone(IST).isoformat(),
        "source_snapshot_ids": dict(snapshot.source_snapshot_ids),
    }


def _expiry_class(raw: str | None) -> ExpiryClass:
    if raw is None:
        return ExpiryClass.WEEKLY
    try:
        return ExpiryClass(str(raw).upper())
    except ValueError:
        return ExpiryClass.WEEKLY


def _spot(snapshot: AgentMarketSnapshot, underlying: str) -> float:
    row = snapshot.underlyings.get(underlying)
    if row is None:
        return 0.0
    if row.spot is not None:
        return float(row.spot)
    if row.ltp is not None:
        return float(row.ltp)
    return 0.0
