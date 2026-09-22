"""Acquire → normalize → PIT → qualify. Fail closed. No fixture fallback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from grow.errors import GrowConfigError
from grow.history.eval import DatasetQualificationRecord, ProviderEvaluationRunner
from grow.history.integrate.contract import AcquireScope, RawArtifact
from grow.history.integrate.normalize import normalize_artifact
from grow.history.integrate.recorded import open_provider
from grow.history.models import APPROVED_FOR_2E, FRAMEWORK_TEST_ONLY, QUALIFIED
from grow.history.qualify_store import DatasetQualificationStore
from grow.history.store import CanonicalStore
from grow.history.universe import default_index_registry


@dataclass(frozen=True)
class IntegrationResult:
    lifecycle: tuple[str, ...]
    artifact: RawArtifact
    store: CanonicalStore
    qualification: DatasetQualificationRecord
    policy_fingerprint: str
    provider_id: str
    adapter_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "lifecycle": list(self.lifecycle),
            "provider_id": self.provider_id,
            "adapter_version": self.adapter_version,
            "artifact_fingerprint": self.artifact.fingerprint,
            "dataset_id": self.store.meta.dataset_id,
            "dataset_version": self.store.meta.version,
            "dataset_fingerprint": self.store.meta.fingerprint,
            "qualification_status": self.qualification.qualification_status,
            "policy_fingerprint": self.policy_fingerprint,
            "coverage_start": self.store.meta.coverage_start.isoformat(),
            "coverage_end": self.store.meta.coverage_end.isoformat(),
            "live": False,
        }


def ingest(
    scope: AcquireScope,
    *,
    payload: Mapping[str, Any],
    max_retries: int = 2,
    qualifications: DatasetQualificationStore | None = None,
) -> IntegrationResult:
    steps = ["RAW_ACQUIRED"]
    provider = open_provider(scope.provider_id)
    artifact = provider.acquire(scope, payload=payload, max_retries=max_retries)
    store = normalize_artifact(artifact)
    steps.append("NORMALIZED")
    if store.meta.is_fixture or store.meta.usage_scope == FRAMEWORK_TEST_ONLY:
        raise GrowConfigError("FIXTURE_FALLBACK_FORBIDDEN")
    store.publish()
    steps.append("PIT_VALIDATED")
    steps.append("QUALIFICATION_REVIEW")
    record = ProviderEvaluationRunner().qualify(store)
    if qualifications is not None:
        qualifications.put(record)
    if record.qualification_status in {QUALIFIED, APPROVED_FOR_2E}:
        steps.append("QUALIFIED")
    else:
        steps.append("REJECTED")
    return IntegrationResult(
        lifecycle=tuple(steps),
        artifact=artifact,
        store=store,
        qualification=record,
        policy_fingerprint=default_index_registry().fingerprint,
        provider_id=provider.identity,
        adapter_version=provider.adapter_version,
    )
