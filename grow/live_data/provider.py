"""Provider-neutral live stream contract. No broker. No HTTP in v1."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from grow.errors import GrowConfigError
from grow.live_data.models import ADAPTER_VERSION, MOCK_PROVIDER_ID, LiveHealth

APPROVED_PROVIDERS = frozenset({"mock"})
_FORBIDDEN_FALLBACK = frozenset(
    {
        "fixture",
        "historical",
        "grow.data.fixture.v1",
        "grow.history.sample.v1",
        "grow.history.recorded.nse_fo.v1",
        "recorded.nse_fo.v1",
    }
)


class LiveDataProvider(Protocol):
    identity: str
    adapter_version: str

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def health(self) -> LiveHealth: ...

    def poll(self) -> Mapping[str, Any] | None: ...


def open_provider(provider_id: str, **kwargs: Any) -> LiveDataProvider:
    name = (provider_id or "").strip().lower()
    if name in _FORBIDDEN_FALLBACK:
        raise GrowConfigError("FIXTURE_FALLBACK_FORBIDDEN")
    if "live" in name and name not in APPROVED_PROVIDERS:
        raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{provider_id}")
    if name not in APPROVED_PROVIDERS:
        raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{provider_id}")
    from grow.live_data.mock import MockStreamProvider

    return MockStreamProvider(**kwargs)


def require_mock_identity(identity: str) -> None:
    if identity != MOCK_PROVIDER_ID:
        raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{identity}")
