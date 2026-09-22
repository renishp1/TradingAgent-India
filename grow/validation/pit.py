"""Point-in-time / future-data leakage guards for historical evaluation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from grow.clock import IST
from grow.errors import GrowConfigError
from grow.market_data.normalized.models import AgentMarketSnapshot, OptionQuoteView


def as_ist(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise GrowConfigError("PIT_NAIVE_TIMESTAMP")
    return moment.astimezone(IST)


def assert_not_future(observed: datetime, decision_at: datetime, *, code: str) -> None:
    if as_ist(observed) > as_ist(decision_at):
        raise GrowConfigError(code)


def bar_is_available(*, bar_end: datetime, decision_at: datetime) -> bool:
    return as_ist(bar_end) <= as_ist(decision_at)


def quote_is_available(quote: OptionQuoteView, decision_at: datetime) -> bool:
    return as_ist(quote.quote_timestamp) <= as_ist(decision_at)


def filter_quotes_pit(
    quotes: Iterable[OptionQuoteView],
    decision_at: datetime,
) -> tuple[OptionQuoteView, ...]:
    return tuple(q for q in quotes if quote_is_available(q, decision_at))


def snapshot_leakage_flags(snapshot: AgentMarketSnapshot) -> tuple[str, ...]:
    """Detect future quotes or inconsistent timestamps on an agent snapshot."""

    decision = as_ist(snapshot.decision_timestamp)
    flags: list[str] = []
    for quote in snapshot.option_contracts:
        if as_ist(quote.quote_timestamp) > decision:
            flags.append("LOOKAHEAD_OPTION_QUOTE")
            break
    for underlying in snapshot.underlyings.values():
        ts = getattr(underlying, "quote_timestamp", None)
        if ts is not None and as_ist(ts) > decision:
            flags.append("LOOKAHEAD_UNDERLYING_QUOTE")
            break
    snap_as_of = getattr(snapshot, "as_of", None)
    if snap_as_of is not None and as_ist(snap_as_of) > decision:
        flags.append("LOOKAHEAD_SNAPSHOT_AS_OF")
    return tuple(flags)


def reject_future_feature(name: str, feature_time: datetime, decision_at: datetime) -> None:
    """Indicators and labels may only use data at or before the decision clock."""

    assert_not_future(feature_time, decision_at, code=f"LOOKAHEAD_FEATURE:{name}")


def assert_no_random_split(method: str) -> None:
    if method.lower() in {"random", "shuffle", "kfold", "stratified"}:
        raise GrowConfigError("RANDOM_SPLIT_FORBIDDEN")


def assert_dataset_versions_match(
    run_dataset: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    for key in ("dataset_id", "dataset_version", "dataset_fingerprint"):
        if expected.get(key) and run_dataset.get(key) != expected.get(key):
            raise GrowConfigError(f"DATASET_VERSION_MISMATCH:{key}")
