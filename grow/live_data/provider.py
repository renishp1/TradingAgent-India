"""Provider-neutral live stream contract. No broker. No HTTP in v1 mock."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from grow.errors import GrowConfigError
from grow.live_data.models import ADAPTER_VERSION, APPROVED_STREAM_IDS, MOCK_PROVIDER_ID, TRUEDATA_PROVIDER_ID, LiveHealth

APPROVED_PROVIDERS = frozenset({"mock", "truedata", "kite_market"})
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


def approved_stream_identity(identity: str) -> bool:
    return identity in APPROVED_STREAM_IDS


def open_provider(provider_id: str, **kwargs: Any) -> LiveDataProvider:
    name = (provider_id or "").strip().lower()
    if name in _FORBIDDEN_FALLBACK:
        raise GrowConfigError("FIXTURE_FALLBACK_FORBIDDEN")
    if "live" in name and name not in APPROVED_PROVIDERS:
        raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{provider_id}")
    if name not in APPROVED_PROVIDERS:
        raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{provider_id}")
    if name == "truedata":
        from grow.live_data.truedata import TrueDataAdapter, settings_from_live_config

        settings = kwargs.pop("settings", None)
        live = kwargs.pop("live_config", None)
        if settings is None and live is not None:
            settings = settings_from_live_config(live)
        return TrueDataAdapter(settings=settings, **kwargs)
    if name == "kite_market":
        from grow.live_data.kite_market import KiteMarketProvider, settings_from_live_config

        settings = kwargs.pop("settings", None)
        live = kwargs.pop("live_config", None)
        kwargs.pop("events", None)
        if settings is None and live is not None:
            settings = settings_from_live_config(live)
        return KiteMarketProvider(settings=settings, **kwargs)
    from grow.live_data.mock import MockStreamProvider

    return MockStreamProvider(**kwargs)


def require_mock_identity(identity: str) -> None:
    if identity != MOCK_PROVIDER_ID:
        raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{identity}")


def require_stream_identity(identity: str) -> None:
    if identity not in APPROVED_STREAM_IDS:
        raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{identity}")