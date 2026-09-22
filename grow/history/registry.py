"""Immutable dataset registry. No secrets. 2F reads capabilities only."""

from __future__ import annotations

from grow.errors import GrowConfigError
from grow.history.sample import build_sample_store
from grow.history.store import CanonicalStore


class DatasetRegistry:
    def __init__(self) -> None:
        self._stores: dict[tuple[str, str], CanonicalStore] = {}

    def register(self, store: CanonicalStore) -> None:
        if not store.meta.fingerprint or store.meta.fingerprint == "pending":
            raise GrowConfigError("UNPUBLISHED_DATASET")
        key = (store.meta.dataset_id, store.meta.version)
        if key in self._stores:
            existing = self._stores[key]
            if existing.meta.fingerprint != store.meta.fingerprint:
                raise GrowConfigError("DATASET_IMMUTABLE")
            return
        if store.meta.license_status not in {"APPROVED", "APPROVED_WITH_WARNINGS"}:
            # still stored, but catalog will mark NOT_APPROVED
            pass
        self._stores[key] = store

    def get(self, dataset_id: str, version: str) -> CanonicalStore:
        key = (dataset_id, version)
        if key not in self._stores:
            raise GrowConfigError("DATASET_UNAVAILABLE")
        return self._stores[key]

    def require_approved(self, dataset_id: str, version: str) -> CanonicalStore:
        store = self.get(dataset_id, version)
        if store.meta.license_status not in {"APPROVED", "APPROVED_WITH_WARNINGS"}:
            raise GrowConfigError(f"DATASET_NOT_APPROVED:{dataset_id}")
        if store.meta.quality_status in {"REJECTED", "RETIRED", "DRAFT"}:
            raise GrowConfigError(f"DATASET_NOT_APPROVED:{dataset_id}")
        return store

    def catalog_rows(self) -> tuple[dict, ...]:
        rows = []
        for store in self._stores.values():
            meta = store.meta
            license_status = meta.license_status if meta.license_status != "APPROVED" else "APPROVED"
            rows.append(
                {
                    "dataset_id": meta.dataset_id,
                    "provider": meta.provider_name,
                    "instrument_scope": meta.instrument_scope,
                    "date_coverage": (meta.coverage_start, meta.coverage_end),
                    "timestamp_granularity": meta.granularity,
                    "timezone": meta.timezone,
                    "option_chain_depth": meta.option_depth,
                    "bid_ask_available": meta.bid_ask_available,
                    "oi_available": meta.oi_available,
                    "volume_available": meta.volume_available,
                    "iv_available": meta.iv_available,
                    "greeks_available": meta.greeks_available,
                    "historical_contract_metadata": meta.contract_metadata_available,
                    "session_calendar_version": meta.calendar_version,
                    "quality_status": meta.quality_status,
                    "licensing_status": license_status,
                    "dataset_version": meta.version,
                    "fingerprint": meta.fingerprint,
                    "provenance": f"{meta.provenance}|fp={meta.fingerprint}",
                }
            )
        return tuple(rows)


_GLOBAL: DatasetRegistry | None = None


def default_registry() -> DatasetRegistry:
    global _GLOBAL
    if _GLOBAL is None:
        _GLOBAL = DatasetRegistry()
        _GLOBAL.register(build_sample_store())
    return _GLOBAL
