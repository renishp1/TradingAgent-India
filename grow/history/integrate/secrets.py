"""Provider credentials from the environment only. Never commit or log secrets."""

from __future__ import annotations

from typing import Any, Mapping

from grow.errors import GrowConfigError

_SECRET_HEADERS = frozenset({"authorization", "x-api-key", "x-auth-token", "api-key", "cookie"})
_SECRET_KEYS = _SECRET_HEADERS | frozenset({"api_key", "apikey", "access_token", "token", "password", "secret"})


def provider_secret(name: str, environ: Mapping[str, str]) -> str:
    key = f"GROW_HISTORICAL_{name}"
    value = environ.get(key, "")
    if not value.strip():
        raise GrowConfigError(f"PROVIDER_SECRET_MISSING:{key}")
    return value


def strip_secret_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        if key.lower() in _SECRET_HEADERS:
            continue
        out[key] = value
    return out


def strip_secrets(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop credentials from a stored artifact payload. Nested header maps are stripped."""
    out: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        if key.lower() in _SECRET_KEYS:
            continue
        if key.lower() in {"response_headers", "headers", "request_headers"} and isinstance(value, Mapping):
            out[key] = strip_secret_headers(value)
            continue
        out[key] = value
    return out
