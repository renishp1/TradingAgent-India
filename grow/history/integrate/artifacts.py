"""Immutable raw-provider artifacts. Auth headers are never stored."""

from __future__ import annotations

from typing import Any, Mapping

from grow.history.fingerprint import fingerprint
from grow.history.integrate.contract import AcquireScope, RawArtifact
from grow.history.integrate.secrets import strip_secret_headers, strip_secrets


def make_artifact(
    *,
    provider_id: str,
    adapter_version: str,
    vendor_schema: str,
    retrieved_at: str,
    scope: AcquireScope,
    payload: Mapping[str, Any],
    retry_count: int = 0,
    rate_limit_remaining: int | None = None,
    headers: Mapping[str, str] | None = None,
) -> RawArtifact:
    safe_payload = strip_secrets(payload)
    safe_headers = strip_secret_headers(headers)
    body = {
        "provider_id": provider_id,
        "adapter_version": adapter_version,
        "vendor_schema": vendor_schema,
        "scope": scope.to_dict(),
        "payload": safe_payload,
    }
    return RawArtifact(
        provider_id=provider_id,
        adapter_version=adapter_version,
        vendor_schema=vendor_schema,
        fingerprint=fingerprint(body),
        retrieved_at=retrieved_at,
        scope=scope,
        payload=safe_payload,
        retry_count=retry_count,
        rate_limit_remaining=rate_limit_remaining,
        headers=safe_headers,
    )
