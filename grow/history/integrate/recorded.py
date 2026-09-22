"""Explicit recorded NSE F&O-shaped adapter. No network. No fixture fallback."""

from __future__ import annotations

from typing import Any, Mapping

from grow.errors import GrowConfigError
from grow.history.integrate.artifacts import make_artifact
from grow.history.integrate.contract import (
    ADAPTER_VERSION,
    RECORDED_PROVIDER_ID,
    VENDOR_SCHEMA,
    AcquireScope,
    RawArtifact,
)

APPROVED_PROVIDERS = frozenset({RECORDED_PROVIDER_ID})
_FORBIDDEN_FALLBACK = frozenset(
    {
        "fixture",
        "grow.data.fixture.v1",
        "grow.history.sample.v1",
        "grow.history.provider.sample.v1",
        "grow.history.eval.sample.v1",
    }
)


class RecordedHistoricalProvider:
    identity = RECORDED_PROVIDER_ID
    adapter_version = ADAPTER_VERSION

    def acquire(
        self,
        scope: AcquireScope,
        *,
        payload: Mapping[str, Any] | None = None,
        max_retries: int = 2,
    ) -> RawArtifact:
        if payload is None:
            raise GrowConfigError("RECORDED_PAYLOAD_REQUIRED")
        provider = str(payload.get("provider") or "")
        if provider in _FORBIDDEN_FALLBACK or scope.provider_id in _FORBIDDEN_FALLBACK:
            raise GrowConfigError("FIXTURE_FALLBACK_FORBIDDEN")
        if "live" in provider.lower() or "live" in scope.provider_id.lower():
            raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{provider or scope.provider_id}")
        if provider != RECORDED_PROVIDER_ID or scope.provider_id != RECORDED_PROVIDER_ID:
            raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{provider or scope.provider_id}")
        if payload.get("vendor_schema") != VENDOR_SCHEMA:
            raise GrowConfigError("UNKNOWN_VENDOR_SCHEMA")
        if payload.get("instrument_type", "OPTIDX") != "OPTIDX":
            raise GrowConfigError("UNSUPPORTED_INSTRUMENT_TYPE")
        failures = int(payload.get("transient_failures") or 0)
        retries = 0
        while failures > 0 and retries < max_retries:
            retries += 1
            failures -= 1
        if failures > 0:
            raise GrowConfigError("PROVIDER_TRANSIENT_EXHAUSTED")
        pages = payload.get("pages")
        if pages is not None and not isinstance(pages, list):
            raise GrowConfigError("MALFORMED_PAGE")
        body: dict[str, Any]
        if pages:
            body = _flatten_pages(payload, pages)
        else:
            body = dict(payload)
        headers = dict(payload.get("response_headers") or payload.get("headers") or {})
        remaining = payload.get("rate_limit_remaining")
        remaining_i = None if remaining is None else int(remaining)
        return make_artifact(
            provider_id=RECORDED_PROVIDER_ID,
            adapter_version=ADAPTER_VERSION,
            vendor_schema=VENDOR_SCHEMA,
            retrieved_at=str(payload.get("retrieved_at") or "2026-09-14T16:00:00+05:30"),
            scope=scope,
            payload=body,
            retry_count=retries,
            rate_limit_remaining=remaining_i,
            headers=headers,
        )


def _flatten_pages(payload: Mapping[str, Any], pages: list) -> dict[str, Any]:
    body = {k: v for k, v in payload.items() if k != "pages"}
    calendar: list = list(body.get("calendar") or [])
    bars: list = list(body.get("spot_bars") or [])
    master: list = list(body.get("contract_master") or [])
    quotes: list = list(body.get("option_quotes") or [])
    for page in pages:
        if not isinstance(page, dict):
            raise GrowConfigError("MALFORMED_PAGE")
        calendar.extend(page.get("calendar") or [])
        bars.extend(page.get("spot_bars") or [])
        master.extend(page.get("contract_master") or [])
        quotes.extend(page.get("option_quotes") or [])
    body["calendar"] = calendar
    body["spot_bars"] = bars
    body["contract_master"] = master
    body["option_quotes"] = quotes
    return body


def open_provider(provider_id: str) -> RecordedHistoricalProvider:
    if provider_id in _FORBIDDEN_FALLBACK:
        raise GrowConfigError("FIXTURE_FALLBACK_FORBIDDEN")
    if provider_id not in APPROVED_PROVIDERS or "live" in provider_id.lower():
        raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{provider_id}")
    return RecordedHistoricalProvider()
