"""Map nse.fo.recorded.v1 onto the canonical PIT store. Never fabricate IV/Greeks."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from grow.errors import GrowConfigError
from grow.history.adapter import _dt
from grow.history.integrate.contract import RECORDED_PROVIDER_ID, VENDOR_SCHEMA, RawArtifact
from grow.history.models import (
    CANDIDATE,
    FRAMEWORK_TEST_ONLY,
    HISTORICAL_RESEARCH,
    MAPPING_EXACT,
    NORM,
    SCHEMA,
    DatasetVersion,
    HistoricalBar,
    HistoricalOptionContract,
    HistoricalOptionQuote,
    HistoricalSession,
)
from grow.history.store import CanonicalStore
from grow.history.universe import is_forbidden_instrument

SOURCE_TZ_REQUIRED = "Asia/Kolkata"


def canonical_contract_id(underlying: str, expiry: str, strike: float, option_type: str) -> str:
    return f"{underlying}-{expiry}-{int(strike)}-{option_type}"


def normalize_artifact(artifact: RawArtifact, *, meta: DatasetVersion | None = None) -> CanonicalStore:
    raw = artifact.payload
    if artifact.provider_id != RECORDED_PROVIDER_ID or raw.get("provider") != RECORDED_PROVIDER_ID:
        raise GrowConfigError("FIXTURE_FALLBACK_FORBIDDEN")
    if artifact.vendor_schema != VENDOR_SCHEMA:
        raise GrowConfigError("UNKNOWN_VENDOR_SCHEMA")
    tz = raw.get("source_timezone")
    if tz != SOURCE_TZ_REQUIRED:
        raise GrowConfigError("SOURCE_TIMEZONE")
    version = meta or _meta_from(raw, artifact)
    if version.is_fixture or version.usage_scope == FRAMEWORK_TEST_ONLY:
        raise GrowConfigError("FIXTURE_FALLBACK_FORBIDDEN")
    store = CanonicalStore(version)
    for row in raw.get("calendar") or []:
        store.add_session(_session(row, version))
    for row in raw.get("spot_bars") or []:
        store.add_bar(_bar(row, version))
    seen_identity: dict[tuple[str, str, float, str], str] = {}
    seen_provider: dict[str, str] = {}
    for row in raw.get("contract_master") or []:
        contract = _contract(row, version)
        ident = contract.identity()
        prior = seen_identity.get(ident)
        if prior is not None and prior != contract.contract_id:
            raise GrowConfigError(f"IDENTITY_AMBIGUITY:{ident}")
        if prior is not None:
            existing = store.contract(contract.contract_id)
            if existing.provider_contract_id != contract.provider_contract_id:
                raise GrowConfigError(f"IDENTITY_AMBIGUITY:{ident}")
            continue
        provider_prior = seen_provider.get(contract.provider_contract_id)
        if provider_prior is not None and provider_prior != contract.contract_id:
            raise GrowConfigError(f"IDENTITY_AMBIGUITY:{contract.provider_contract_id}")
        seen_identity[ident] = contract.contract_id
        seen_provider[contract.provider_contract_id] = contract.contract_id
        store.add_contract(contract)
    for row in raw.get("option_quotes") or []:
        store.add_quote(_quote(row, version))
    return store


def _aware(value: str, label: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.tzinfo.utcoffset(stamp) is None:
        raise GrowConfigError(f"NAIVE_TIMESTAMP:{label}")
    return _dt(value)


def _meta_from(raw: Mapping[str, Any], artifact: RawArtifact) -> DatasetVersion:
    coverage = raw.get("coverage") or {}
    return DatasetVersion(
        dataset_id=str(raw.get("dataset_id") or "grow.history.recorded.nse_fo.v1"),
        version=str(raw.get("dataset_version") or "2026.09.2j.a"),
        source_version=artifact.fingerprint[:12],
        normalization_version=NORM,
        schema_version=SCHEMA,
        calendar_version=str(raw.get("calendar_version") or "nse.session.recorded.v1"),
        coverage_start=date.fromisoformat(coverage["start"]),
        coverage_end=date.fromisoformat(coverage["end"]),
        fingerprint="pending",
        published_at=artifact.retrieved_at,
        quality_status=str(raw.get("quality_status") or "APPROVED"),
        license_status=str(raw.get("license_status") or "APPROVED"),
        source_id=RECORDED_PROVIDER_ID,
        provider_name=RECORDED_PROVIDER_ID,
        granularity=tuple(raw.get("granularity") or ("M15", "D1")),
        instrument_scope=tuple(raw.get("underlyings") or ()),
        bid_ask_available=True,
        oi_available=True,
        volume_available=True,
        iv_available=bool(raw.get("iv_available", False)),
        greeks_available=bool(raw.get("greeks_available", False)),
        contract_metadata_available=True,
        option_depth="atm_pm2",
        provenance=f"recorded artifact {artifact.fingerprint} adapter={artifact.adapter_version}",
        timezone="Asia/Kolkata",
        usage_scope=str(raw.get("usage_scope") or HISTORICAL_RESEARCH),
        is_fixture=False,
        snapshot_cadence=tuple(raw.get("snapshot_cadence") or ("11:00", "15:15")),
        quality_warnings=tuple(raw.get("quality_warnings") or ()),
        mapping_policy=MAPPING_EXACT,
        slot_tolerance_seconds=0,
        qualification_status=CANDIDATE,
    )


def _session(row: Mapping[str, Any], meta: DatasetVersion) -> HistoricalSession:
    return HistoricalSession(
        session_date=date.fromisoformat(row["date"]),
        open_at=_aware(row["open"], "session.open"),
        close_at=_aware(row["close"], "session.close"),
        source=meta.source_id,
        calendar_version=meta.calendar_version,
        status=str(row.get("status") or "OPEN"),
        special_reason=row.get("special_reason"),
    )


def _bar(row: Mapping[str, Any], meta: DatasetVersion) -> HistoricalBar:
    symbol = str(row["underlying"])
    if is_forbidden_instrument(symbol):
        raise GrowConfigError(f"UNSUPPORTED_UNDERLYING:{symbol}")
    start = _aware(row["start"], "bar.start")
    end = _aware(row["end"], "bar.end")
    return HistoricalBar(
        symbol=symbol,
        timeframe=str(row["timeframe"]),
        timestamp=start,
        end=end,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=int(row["volume"]),
        source_id=meta.source_id,
        dataset_version=meta.version,
        as_of_available_at=end,
        corporate_action_adjustment_version="unadjusted.v1",
        quality_flags=(),
    )


def _contract(row: Mapping[str, Any], meta: DatasetVersion) -> HistoricalOptionContract:
    if str(row.get("instrument_type") or "OPTIDX") != "OPTIDX":
        raise GrowConfigError(f"UNSUPPORTED_INSTRUMENT_TYPE:{row.get('instrument_type')}")
    underlying = str(row["underlying"])
    if is_forbidden_instrument(underlying):
        raise GrowConfigError(f"UNSUPPORTED_UNDERLYING:{underlying}")
    option_type = str(row["option_type"]).upper()
    if option_type not in {"CE", "PE"}:
        raise GrowConfigError(f"UNSUPPORTED_OPTION_TYPE:{option_type}")
    expiry = str(row["expiry"])
    strike = float(row["strike"])
    provider_id = str(row.get("tradingsymbol") or row.get("provider_contract_id") or "")
    cid = canonical_contract_id(underlying, expiry, strike, option_type)
    if not provider_id:
        raise GrowConfigError("MISSING_PROVIDER_CONTRACT_ID")
    lot = row.get("lot_size")
    if lot is None:
        raise GrowConfigError("MISSING_LOT_SIZE")
    return HistoricalOptionContract(
        underlying=underlying,
        expiry=date.fromisoformat(expiry),
        strike=strike,
        option_type=option_type,
        contract_id=cid,
        provider_contract_id=provider_id,
        lot_size=int(lot),
        expiry_class=str(row.get("expiry_class") or "WEEKLY"),
        first_seen_at=_aware(row["listed_from"], "contract.listed_from"),
        last_seen_at=_aware(row["listed_to"], "contract.listed_to"),
        listing_status=str(row.get("listing_status") or "ACTIVE"),
        source_id=meta.source_id,
        dataset_version=meta.version,
    )


def _quote(row: Mapping[str, Any], meta: DatasetVersion) -> HistoricalOptionQuote:
    expiry = str(row["expiry"])
    cid = canonical_contract_id(str(row["underlying"]), expiry, float(row["strike"]), str(row["option_type"]).upper())
    ts = _aware(row["ts"], "quote.ts")
    iv = row.get("iv")
    delta = row.get("delta")
    return HistoricalOptionQuote(
        contract_id=cid,
        timestamp=ts,
        bid=None if row.get("bid") is None else float(row["bid"]),
        ask=None if row.get("ask") is None else float(row["ask"]),
        ltp=None if row.get("ltp") is None else float(row["ltp"]),
        volume=None if row.get("volume") is None else int(row["volume"]),
        open_interest=None if row.get("oi") is None else int(row["oi"]),
        previous_open_interest=None if row.get("poi") is None else int(row["poi"]),
        implied_volatility=None if iv is None else float(iv),
        delta=None if delta is None else float(delta),
        gamma=None if row.get("gamma") is None else float(row["gamma"]),
        theta=None if row.get("theta") is None else float(row["theta"]),
        vega=None if row.get("vega") is None else float(row["vega"]),
        greek_source=None if delta is None else "PROVIDER",
        iv_source=None if iv is None else "PROVIDER",
        source_id=meta.source_id,
        dataset_version=meta.version,
        as_of_available_at=ts,
        quality_flags=(),
    )
