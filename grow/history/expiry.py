"""Historical expiry universe and nearest-weekly reconstruction.

Does not replace 2C. 2C remains the only selector used at decision time.
This module reconstructs the universe that was available at as_of and
applies the same locked v1 rule: future WEEKLY, same-day excluded.
"""

from __future__ import annotations

from datetime import date, datetime

from grow.clock import IST
from grow.history.models import HistoricalExpiryRecord, HistoricalOptionContract
from grow.history.store import CanonicalStore
from grow.options.models import ExpiryClass, OptionChainSnapshot, OptionExpiry
from grow.options.select import choose_expiry
from grow.config import OptionsConfig, load_config

NO_TRADE = "NO_TRADE"


def expiry_records(store: CanonicalStore, underlying: str) -> tuple[HistoricalExpiryRecord, ...]:
    grouped: dict[tuple[str, date, str], list[HistoricalOptionContract]] = {}
    for contract in store.all_contracts():
        if contract.underlying != underlying:
            continue
        key = (contract.underlying, contract.expiry, contract.expiry_class)
        grouped.setdefault(key, []).append(contract)
    rows: list[HistoricalExpiryRecord] = []
    for (und, expiry, klass), items in grouped.items():
        rows.append(
            HistoricalExpiryRecord(
                underlying=und,
                expiry=expiry,
                expiry_class=klass,
                first_seen_at=min(c.first_seen_at for c in items),
                last_seen_at=max(c.last_seen_at for c in items),
                listing_status=items[0].listing_status,
                source_id=items[0].source_id,
                dataset_version=items[0].dataset_version,
            )
        )
    return tuple(sorted(rows, key=lambda r: (r.expiry, r.expiry_class)))


def universe_at(
    store: CanonicalStore,
    underlying: str,
    as_of: datetime,
) -> tuple[HistoricalExpiryRecord, ...]:
    moment = as_of.astimezone(IST)
    return tuple(
        rec
        for rec in expiry_records(store, underlying)
        if rec.first_seen_at <= moment <= rec.last_seen_at
    )


def select_nearest_weekly_expiry(
    underlying: str,
    as_of: datetime,
    historical_expiries: tuple[HistoricalExpiryRecord, ...],
    *,
    allow_same_day: bool = False,
) -> date | None:
    from grow.history.resolver import resolve_nearest_expiry
    from grow.history.universe import WEEKLY_PREFERRED

    resolution = resolve_nearest_expiry(
        underlying,
        as_of,
        historical_expiries,
        WEEKLY_PREFERRED,
        allow_same_day=allow_same_day,
    )
    if resolution.selected_expiry is None:
        return None
    return date.fromisoformat(resolution.selected_expiry)


def nearest_weekly_from_chain(chain: OptionChainSnapshot, as_of: datetime, config: OptionsConfig | None = None) -> date | None:
    """2C selection on a reconstructed historical chain. Single source of decision policy."""
    cfg = config or load_config().options
    chosen, _why = choose_expiry(chain, as_of, cfg)
    return None if chosen is None else chosen.day


def expiries_to_option_expiries(records: tuple[HistoricalExpiryRecord, ...]) -> tuple[OptionExpiry, ...]:
    out: list[OptionExpiry] = []
    for rec in records:
        klass = ExpiryClass.WEEKLY if rec.expiry_class == "WEEKLY" else ExpiryClass.MONTHLY
        out.append(OptionExpiry(rec.expiry, klass))
    return tuple(out)
