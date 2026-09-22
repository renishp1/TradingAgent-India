"""In-memory DatasetQualificationStore. Does not mutate CanonicalStore."""

from __future__ import annotations

from grow.errors import GrowConfigError
from grow.history.eval import DatasetQualificationRecord, QUALIFICATION_SCHEMA


class DatasetQualificationStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], DatasetQualificationRecord] = {}

    def put(self, record: DatasetQualificationRecord) -> DatasetQualificationRecord:
        if record.schema != QUALIFICATION_SCHEMA:
            raise GrowConfigError("QUALIFICATION_SCHEMA")
        key = (record.dataset_id, record.dataset_version, record.fingerprint)
        self._rows[key] = record
        return record

    def get(self, dataset_id: str, version: str, fingerprint: str) -> DatasetQualificationRecord:
        key = (dataset_id, version, fingerprint)
        if key not in self._rows:
            raise GrowConfigError("QUALIFICATION_NOT_FOUND")
        return self._rows[key]
