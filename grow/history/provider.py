"""Vendor-neutral ingest. Maps a machine-readable export to CanonicalStore.

No live API. No secrets. Fixture/eval mapping remains EXACT.
"""

from __future__ import annotations

from grow.errors import GrowConfigError
from grow.history.adapter import load_payload
from grow.history.models import MAPPING_EXACT, DatasetVersion
from grow.history.store import CanonicalStore

VENDOR_SCHEMA = "grow.history.provider.export.v1"


def export_vendor_payload(store: CanonicalStore) -> dict[str, Any]:
    """Provider-native shape distinct from the 2G canonical dump."""
    meta = store.meta
    return {
        "vendor": meta.provider_name,
        "vendor_schema": VENDOR_SCHEMA,
        "license": meta.license_status,
        "source_timezone": "Asia/Kolkata",
        "mapping_policy": meta.mapping_policy,
        "slot_tolerance_seconds": meta.slot_tolerance_seconds,
        "cadence": list(meta.snapshot_cadence),
        "dataset_id": meta.dataset_id,
        "dataset_version": meta.version,
        "calendar_version": meta.calendar_version,
        "sessions": [
            {
                "date": s.session_date.isoformat(),
                "open": s.open_at.isoformat(),
                "close": s.close_at.isoformat(),
                "status": s.status,
                "special_reason": s.special_reason,
                "source": s.source,
                "calendar_version": s.calendar_version,
            }
            for s in sorted(store._sessions.values(), key=lambda x: x.session_date)
        ],
        "underlyings": [
            {
                "symbol": b.symbol,
                "timeframe": b.timeframe,
                "start": b.timestamp.isoformat(),
                "end": b.end.isoformat(),
                "o": b.open,
                "h": b.high,
                "l": b.low,
                "c": b.close,
                "v": b.volume,
            }
            for b in store.all_bars()
        ],
        "instruments": [
            {
                "id": c.contract_id,
                "underlying": c.underlying,
                "expiry": c.expiry.isoformat(),
                "strike": c.strike,
                "right": c.option_type,
                "lot": c.lot_size,
                "class": c.expiry_class,
                "listed_from": c.first_seen_at.isoformat(),
                "listed_to": c.last_seen_at.isoformat(),
                "listing_status": c.listing_status,
                "provider_contract_id": c.provider_contract_id,
                "source_id": c.source_id,
                "dataset_version": c.dataset_version,
            }
            for c in store.all_contracts()
        ],
        "quotes": [
            {
                "instrument_id": q.contract_id,
                "ts": q.timestamp.isoformat(),
                "bid": q.bid,
                "ask": q.ask,
                "last": q.ltp,
                "vol": q.volume,
                "oi": q.open_interest,
                "previous_open_interest": q.previous_open_interest,
                "implied_volatility": q.implied_volatility,
                "iv_source": q.iv_source,
                "delta": q.delta,
                "gamma": q.gamma,
                "theta": q.theta,
                "vega": q.vega,
                "greek_source": q.greek_source,
                "as_of_available_at": q.as_of_available_at.isoformat(),
                "quality_flags": list(q.quality_flags),
                "source_id": q.source_id,
                "dataset_version": q.dataset_version,
            }
            for q in store.all_quotes()
        ],
    }


def ingest_vendor_payload(raw: dict[str, Any], *, meta: DatasetVersion | None = None) -> CanonicalStore:
    if raw.get("vendor_schema") != VENDOR_SCHEMA:
        raise GrowConfigError("UNKNOWN_VENDOR_SCHEMA")
    if raw.get("mapping_policy", MAPPING_EXACT) != MAPPING_EXACT:
        # v1 ingest is EXACT; other policies are reserved for a licensed adapter.
        raise GrowConfigError("VENDOR_MAPPING_NOT_IMPLEMENTED")
    source_meta = meta
    if source_meta is None:
        raise GrowConfigError("VENDOR_META_REQUIRED")
    canonical = {
        "meta": {
            **source_meta.to_dict(),
            "source_timezone": raw.get("source_timezone") or "Asia/Kolkata",
            "fingerprint": "pending",
        },
        "sessions": [
            {
                "session_date": s["date"],
                "open_at": s["open"],
                "close_at": s["close"],
                "source": s.get("source") or source_meta.source_id,
                "calendar_version": s.get("calendar_version") or source_meta.calendar_version,
                "status": s["status"],
                "special_reason": s.get("special_reason"),
            }
            for s in raw.get("sessions", [])
        ],
        "bars": [
            {
                "symbol": b["symbol"],
                "timeframe": b["timeframe"],
                "timestamp": b["start"],
                "end": b["end"],
                "open": b["o"],
                "high": b["h"],
                "low": b["l"],
                "close": b["c"],
                "volume": b["v"],
                "source_id": source_meta.source_id,
                "dataset_version": source_meta.version,
                "as_of_available_at": b["end"],
                "corporate_action_adjustment_version": "unadjusted.v1",
                "quality_flags": [],
            }
            for b in raw.get("underlyings", [])
        ],
        "contracts": [
            {
                "underlying": c["underlying"],
                "expiry": c["expiry"],
                "strike": c["strike"],
                "option_type": c["right"],
                "contract_id": c["id"],
                "provider_contract_id": c.get("provider_contract_id") or c["id"],
                "lot_size": c["lot"],
                "expiry_class": c["class"],
                "first_seen_at": c["listed_from"],
                "last_seen_at": c["listed_to"],
                "listing_status": c.get("listing_status") or "ACTIVE",
                "source_id": c.get("source_id") or source_meta.source_id,
                "dataset_version": c.get("dataset_version") or source_meta.version,
            }
            for c in raw.get("instruments", [])
        ],
        "quotes": [
            {
                "contract_id": q["instrument_id"],
                "timestamp": q["ts"],
                "bid": q.get("bid"),
                "ask": q.get("ask"),
                "ltp": q.get("last"),
                "volume": q.get("vol"),
                "open_interest": q.get("oi"),
                "previous_open_interest": q.get("previous_open_interest"),
                "implied_volatility": q.get("implied_volatility", q.get("iv")),
                "delta": q.get("delta"),
                "gamma": q.get("gamma"),
                "theta": q.get("theta"),
                "vega": q.get("vega"),
                "greek_source": q.get("greek_source"),
                "iv_source": q.get("iv_source"),
                "source_id": q.get("source_id") or source_meta.source_id,
                "dataset_version": q.get("dataset_version") or source_meta.version,
                "as_of_available_at": q.get("as_of_available_at") or q["ts"],
                "quality_flags": list(q.get("quality_flags") or ()),
            }
            for q in raw.get("quotes", [])
        ],
    }
    return load_payload(canonical)
