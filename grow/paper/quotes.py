"""Map an agent snapshot onto the 3B live snapshot used for marks and exits."""

from __future__ import annotations

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


def live_snapshot_from_agent(snapshot: AgentMarketSnapshot, *, sequence: int) -> LiveSnapshot:
    """Quotes later than the snapshot decision time are omitted. Nothing is fabricated."""
    as_of = snapshot.decision_timestamp.astimezone(IST)
    grouped: dict[str, list[OptionContract]] = {}
    expiries: dict[str, list[OptionExpiry]] = {}
    for quote in snapshot.option_contracts:
        if quote.quality is not DataQualityStatus.OK or not quote_known_at(quote, as_of):
            continue
        contract = OptionContract(
            underlying=quote.underlying,
            expiry=quote.expiry,
            expiry_class=ExpiryClass.WEEKLY,
            strike=quote.strike,
            option_type=OptionType(quote.option_type),
            bid=quote.bid,
            ask=quote.ask,
            last_price=quote.ltp,
            volume=0 if quote.volume is None else quote.volume,
            open_interest=0 if quote.open_interest is None else quote.open_interest,
            previous_open_interest=None,
            implied_volatility=None,
            delta=None,
            gamma=None,
            theta=None,
            vega=None,
            timestamp=quote.quote_timestamp.astimezone(IST),
            provider_contract_id=quote.provider_contract_id,
        )
        grouped.setdefault(quote.underlying, []).append(contract)
        expiries.setdefault(quote.underlying, [])
        marker = OptionExpiry(quote.expiry, ExpiryClass.WEEKLY)
        if marker not in expiries[quote.underlying]:
            expiries[quote.underlying].append(marker)
    chains = {
        underlying: OptionChainSnapshot(
            snapshot_id=snapshot.snapshot_id,
            underlying=underlying,
            as_of=as_of,
            spot=_spot(snapshot, underlying),
            expiries=tuple(expiries.get(underlying, ())),
            contracts=tuple(contracts),
            source_id=snapshot.provider,
            is_fixture=True,
            provider_metadata={"paper_execution": True, "live_trading": False},
        )
        for underlying, contracts in grouped.items()
    }
    return LiveSnapshot(
        snapshot_id=snapshot.snapshot_id,
        schema=SCHEMA,
        provider_id=snapshot.provider,
        adapter_version="paper.execution.v1",
        sequence=sequence,
        event_time=as_of,
        received_time=as_of,
        session_date=snapshot.session_date,
        underlyings=tuple(snapshot.underlyings),
        market={},
        chains=chains,
        lot_sizes={},
        freshness_ok=snapshot.data_quality is DataQualityStatus.OK,
        diagnostics=snapshot.quality_notes,
    )


def _spot(snapshot: AgentMarketSnapshot, underlying: str) -> float:
    row = snapshot.underlyings.get(underlying)
    if row is None:
        return 0.0
    if row.spot is not None:
        return float(row.spot)
    if row.ltp is not None:
        return float(row.ltp)
    return 0.0
