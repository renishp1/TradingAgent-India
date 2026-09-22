"""File/JSON historical adapter. No live vendor. No secrets."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from grow.clock import IST
from grow.errors import GrowConfigError
from grow.history.models import (
    FRAMEWORK_TEST_ONLY,
    DatasetVersion,
    HistoricalBar,
    HistoricalOptionContract,
    HistoricalOptionQuote,
    HistoricalSession,
)
from grow.history.store import CanonicalStore


def _dt(value: str, *, source_tz: str | None = None) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.tzinfo.utcoffset(stamp) is None:
        if not source_tz:
            raise GrowConfigError("NAIVE_TIMESTAMP:adapter")
        stamp = stamp.replace(tzinfo=ZoneInfo(source_tz))
    return stamp.astimezone(IST)


def load_json(path: str | Path) -> CanonicalStore:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return load_payload(raw)


def load_payload(raw: dict) -> CanonicalStore:
    meta_raw = raw["meta"]
    source_tz = meta_raw.get("source_timezone")
    meta = DatasetVersion(
        dataset_id=meta_raw["dataset_id"],
        version=meta_raw["version"],
        source_version=meta_raw.get("source_version", "file"),
        normalization_version=meta_raw.get("normalization_version", "history.normalize.v1"),
        schema_version=meta_raw.get("schema_version", "grow.history.canonical.v1"),
        calendar_version=meta_raw["calendar_version"],
        coverage_start=date.fromisoformat(meta_raw["coverage_start"]),
        coverage_end=date.fromisoformat(meta_raw["coverage_end"]),
        fingerprint="pending",
        published_at=meta_raw.get("published_at", "2026-01-01T08:00:00+05:30"),
        quality_status=meta_raw.get("quality_status", "DRAFT"),
        license_status=meta_raw.get("license_status", "NOT_APPROVED"),
        source_id=meta_raw.get("source_id", "file"),
        provider_name=meta_raw.get("provider_name", "file"),
        granularity=tuple(meta_raw.get("granularity", ["M15", "D1"])),
        instrument_scope=tuple(meta_raw.get("instrument_scope", ["NIFTY", "BANKNIFTY"])),
        bid_ask_available=bool(meta_raw.get("bid_ask_available", True)),
        oi_available=bool(meta_raw.get("oi_available", True)),
        volume_available=bool(meta_raw.get("volume_available", True)),
        iv_available=bool(meta_raw.get("iv_available", False)),
        greeks_available=bool(meta_raw.get("greeks_available", False)),
        contract_metadata_available=bool(meta_raw.get("contract_metadata_available", True)),
        option_depth=str(meta_raw.get("option_depth", "atm_pm2")),
        provenance=str(meta_raw.get("provenance", "file")),
        timezone="Asia/Kolkata",
        usage_scope=str(meta_raw.get("usage_scope", FRAMEWORK_TEST_ONLY)),
        is_fixture=bool(meta_raw.get("is_fixture", False)),
        snapshot_cadence=tuple(meta_raw.get("snapshot_cadence") or ()),
    )
    store = CanonicalStore(meta)
    for row in raw.get("sessions", []):
        store.add_session(
            HistoricalSession(
                session_date=date.fromisoformat(row["session_date"]),
                open_at=_dt(row["open_at"], source_tz=source_tz),
                close_at=_dt(row["close_at"], source_tz=source_tz),
                source=row.get("source", meta.source_id),
                calendar_version=row["calendar_version"],
                status=row["status"],
                special_reason=row.get("special_reason"),
            )
        )
    for row in raw.get("bars", []):
        store.add_bar(
            HistoricalBar(
                symbol=row["symbol"],
                timeframe=row["timeframe"],
                timestamp=_dt(row["timestamp"], source_tz=source_tz),
                end=_dt(row["end"], source_tz=source_tz),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
                source_id=row.get("source_id", meta.source_id),
                dataset_version=meta.version,
                as_of_available_at=_dt(row["as_of_available_at"], source_tz=source_tz),
                corporate_action_adjustment_version=row.get("corporate_action_adjustment_version", "unadjusted.v1"),
                quality_flags=tuple(row.get("quality_flags") or ()),
            )
        )
    for row in raw.get("contracts", []):
        store.add_contract(
            HistoricalOptionContract(
                underlying=row["underlying"],
                expiry=date.fromisoformat(row["expiry"]),
                strike=float(row["strike"]),
                option_type=row["option_type"],
                contract_id=row["contract_id"],
                provider_contract_id=row.get("provider_contract_id", row["contract_id"]),
                lot_size=None if row.get("lot_size") is None else int(row["lot_size"]),
                expiry_class=row.get("expiry_class", "WEEKLY"),
                first_seen_at=_dt(row["first_seen_at"], source_tz=source_tz),
                last_seen_at=_dt(row["last_seen_at"], source_tz=source_tz),
                listing_status=row.get("listing_status", "ACTIVE"),
                source_id=row.get("source_id", meta.source_id),
                dataset_version=meta.version,
            )
        )
    for row in raw.get("quotes", []):
        store.add_quote(
            HistoricalOptionQuote(
                contract_id=row["contract_id"],
                timestamp=_dt(row["timestamp"], source_tz=source_tz),
                bid=None if row.get("bid") is None else float(row["bid"]),
                ask=None if row.get("ask") is None else float(row["ask"]),
                ltp=None if row.get("ltp") is None else float(row["ltp"]),
                volume=None if row.get("volume") is None else int(row["volume"]),
                open_interest=None if row.get("open_interest") is None else int(row["open_interest"]),
                previous_open_interest=row.get("previous_open_interest"),
                implied_volatility=row.get("implied_volatility"),
                delta=row.get("delta"),
                gamma=row.get("gamma"),
                theta=row.get("theta"),
                vega=row.get("vega"),
                greek_source=row.get("greek_source"),
                iv_source=row.get("iv_source"),
                source_id=row.get("source_id", meta.source_id),
                dataset_version=meta.version,
                as_of_available_at=_dt(row["as_of_available_at"], source_tz=source_tz),
                quality_flags=tuple(row.get("quality_flags") or ()),
            )
        )
    store.publish()
    return store
