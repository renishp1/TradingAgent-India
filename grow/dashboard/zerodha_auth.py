"""Zerodha Kite Connect market-data auth for the paper dashboard.

Login + request_token exchange only. Never places orders.
Never returns API keys, secrets, request tokens, or access tokens to callers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from grow.errors import GrowConfigError
from grow.execution.lock import LIVE_TRADING_COMPILED

KITE_LOGIN_URL = "https://kite.zerodha.com/connect/login"
KITE_SESSION_URL = "https://api.kite.trade/session/token"
KITE_PROFILE_URL = "https://api.kite.trade/user/profile"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/auth/zerodha/callback"

_ENV_KEYS = ("KITE_API_KEY", "KITE_API_SECRET", "KITE_ACCESS_TOKEN")


@dataclass(frozen=True)
class ZerodhaAuthStatus:
    api_key_present: bool
    api_secret_present: bool
    access_token_present: bool
    status: str  # CONNECTED | DISCONNECTED | NOT_CONFIGURED
    can_connect: bool
    message: str
    redirect_uri: str = DEFAULT_REDIRECT_URI

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "provider": "zerodha",
            "purpose": "market_data_only",
            "api_key_present": self.api_key_present,
            "api_secret_present": self.api_secret_present,
            "access_token_present": self.access_token_present,
            "status": self.status,
            "can_connect": self.can_connect,
            "connect_path": "/api/zerodha/connect",
            "disconnect_path": "/api/zerodha/disconnect",
            "credentials_path": "/api/zerodha/credentials",
            "storage": "local_dotenv",
            "redirect_uri": self.redirect_uri,
            "message": self.message,
            "live_trading": False,
            "broker_order_path": False,
        }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_dotenv_path(environ: Mapping[str, str] | None = None) -> Path:
    """Prefer cwd .env, else repo-root .env (same order as apply_dotenv)."""
    del environ  # path discovery is filesystem-based
    cwd = Path.cwd() / ".env"
    if cwd.is_file():
        return cwd
    root = _repo_root() / ".env"
    return root


def _read_present(environ: Mapping[str, str], key: str) -> bool:
    return bool(str(environ.get(key) or "").strip())


def _get_secret(environ: Mapping[str, str], key: str) -> str:
    return str(environ.get(key) or "").strip()


def login_url(*, api_key: str, redirect_params: bool = True) -> str:
    if not api_key:
        raise GrowConfigError("AUTH_MISSING")
    query = {"v": "3", "api_key": api_key}
    # Kite uses the app's registered redirect; api_key selects the app.
    del redirect_params
    return f"{KITE_LOGIN_URL}?{urllib.parse.urlencode(query)}"


def build_connect_redirect(environ: Mapping[str, str] | None = None) -> str:
    """Return the Kite login URL for the configured API key."""
    if LIVE_TRADING_COMPILED:
        raise GrowConfigError("LIVE_TRADING_COMPILED")
    env = dict(os.environ if environ is None else environ)
    api_key = _get_secret(env, "KITE_API_KEY")
    api_secret = _get_secret(env, "KITE_API_SECRET")
    if not api_key:
        raise GrowConfigError("AUTH_MISSING: set KITE_API_KEY in .env")
    if not api_secret:
        raise GrowConfigError("AUTH_MISSING: set KITE_API_SECRET in .env")
    return login_url(api_key=api_key)


def _checksum(api_key: str, request_token: str, api_secret: str) -> str:
    raw = f"{api_key}{request_token}{api_secret}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def exchange_request_token(
    request_token: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Exchange request_token for access_token. Returns access_token only."""
    if LIVE_TRADING_COMPILED:
        raise GrowConfigError("LIVE_TRADING_COMPILED")
    token = str(request_token or "").strip()
    if not token:
        raise GrowConfigError("AUTH_FAILED: missing request_token")
    if not re.fullmatch(r"[A-Za-z0-9]+", token):
        raise GrowConfigError("AUTH_FAILED: invalid request_token")

    env = dict(os.environ if environ is None else environ)
    api_key = _get_secret(env, "KITE_API_KEY")
    api_secret = _get_secret(env, "KITE_API_SECRET")
    if not api_key or not api_secret:
        raise GrowConfigError("AUTH_MISSING")

    body = urllib.parse.urlencode(
        {
            "api_key": api_key,
            "request_token": token,
            "checksum": _checksum(api_key, token, api_secret),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        KITE_SESSION_URL,
        data=body,
        method="POST",
        headers={
            "X-Kite-Version": "3",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
            err = str(detail.get("error_type") or detail.get("message") or "AUTH_FAILED")
        except Exception:
            err = "AUTH_FAILED"
        # Public codes only — never echo Zerodha bodies that might echo tokens.
        if exc.code in {401, 403}:
            raise GrowConfigError("AUTH_FAILED") from None
        raise GrowConfigError(f"AUTH_FAILED:{err}") from None
    except urllib.error.URLError as exc:
        raise GrowConfigError("NETWORK_ERROR") from exc

    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise GrowConfigError("AUTH_FAILED")
    data = payload.get("data") or {}
    access = str(data.get("access_token") or "").strip()
    if not access:
        raise GrowConfigError("AUTH_FAILED")
    return access


def upsert_dotenv_value(path: Path, key: str, value: str) -> None:
    """Create or replace KEY=value in a .env file. Never logs the value."""
    if key not in _ENV_KEYS and key != "KITE_API_SECRET":
        raise GrowConfigError("REFUSED: unsupported env key")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    replaced = False
    out: list[str] = []
    for line in lines:
        if pattern.match(line):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(f"# Zerodha market-data (dashboard Connect)")
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def persist_broker_credentials(
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
    environ: dict[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> dict[str, Any]:
    """Store API key / secret on the local machine (.env). Never returns secret values."""
    if LIVE_TRADING_COMPILED:
        raise GrowConfigError("LIVE_TRADING_COMPILED")
    path = dotenv_path or resolve_dotenv_path(environ)
    key = None if api_key is None else str(api_key).strip()
    secret = None if api_secret is None else str(api_secret).strip()

    if key is not None and key != "":
        if not re.fullmatch(r"[A-Za-z0-9]{6,64}", key):
            raise GrowConfigError("AUTH_FAILED: invalid api_key format")
        upsert_dotenv_value(path, "KITE_API_KEY", key)
        if environ is None:
            os.environ["KITE_API_KEY"] = key
        else:
            environ["KITE_API_KEY"] = key
            os.environ["KITE_API_KEY"] = key

    if secret is not None and secret != "":
        if not re.fullmatch(r"[A-Za-z0-9]{6,128}", secret):
            raise GrowConfigError("AUTH_FAILED: invalid api_secret format")
        upsert_dotenv_value(path, "KITE_API_SECRET", secret)
        if environ is None:
            os.environ["KITE_API_SECRET"] = secret
        else:
            environ["KITE_API_SECRET"] = secret
            os.environ["KITE_API_SECRET"] = secret

    env = dict(os.environ if environ is None else environ)
    # Re-read from file into env if we only had path writes and environ was a copy
    if environ is not None:
        env.update(environ)
    st = auth_status(env, probe=False)
    return {
        "ok": True,
        "stored": True,
        "storage": "local_dotenv",
        "storage_path_name": path.name,
        "api_key_present": st.api_key_present,
        "api_secret_present": st.api_secret_present,
        "access_token_present": st.access_token_present,
        "can_connect": st.can_connect,
        "status": st.status,
        "message": (
            "Broker credentials saved on this machine. "
            "Values are not shown again. Click Connect to authorise."
            if st.can_connect
            else "Saved what was provided. Both API key and API secret are required to Connect."
        ),
        "live_trading": False,
        "broker_order_path": False,
    }


def persist_access_token(
    access_token: str,
    *,
    environ: dict[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> Path:
    """Write access token to .env and process environ. Returns path written."""
    token = str(access_token or "").strip()
    if not token:
        raise GrowConfigError("AUTH_FAILED")
    path = dotenv_path or resolve_dotenv_path(environ)
    upsert_dotenv_value(path, "KITE_ACCESS_TOKEN", token)
    if environ is None:
        os.environ["KITE_ACCESS_TOKEN"] = token
    else:
        environ["KITE_ACCESS_TOKEN"] = token
        os.environ["KITE_ACCESS_TOKEN"] = token
    return path


def clear_access_token(
    *,
    environ: dict[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> Path:
    """Remove access token from .env and process environ."""
    path = dotenv_path or resolve_dotenv_path(environ)
    upsert_dotenv_value(path, "KITE_ACCESS_TOKEN", "")
    if environ is None:
        os.environ.pop("KITE_ACCESS_TOKEN", None)
    else:
        environ.pop("KITE_ACCESS_TOKEN", None)
        os.environ.pop("KITE_ACCESS_TOKEN", None)
    # Rewrite empty assignment as commented absence for clarity
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        text = re.sub(
            r"(?m)^\s*KITE_ACCESS_TOKEN\s*=\s*.*$",
            "# KITE_ACCESS_TOKEN=",
            text,
        )
        path.write_text(text, encoding="utf-8")
    return path


def probe_session(environ: Mapping[str, str] | None = None) -> bool:
    """True if current api_key + access_token authenticate against /user/profile."""
    env = dict(os.environ if environ is None else environ)
    api_key = _get_secret(env, "KITE_API_KEY")
    access = _get_secret(env, "KITE_ACCESS_TOKEN")
    if not api_key or not access:
        return False
    req = urllib.request.Request(
        KITE_PROFILE_URL,
        headers={
            "Authorization": f"token {api_key}:{access}",
            "X-Kite-Version": "3",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return isinstance(payload, dict) and payload.get("status") == "success"
    except Exception:
        return False


def auth_status(environ: Mapping[str, str] | None = None, *, probe: bool = False) -> ZerodhaAuthStatus:
    env = dict(os.environ if environ is None else environ)
    key_ok = _read_present(env, "KITE_API_KEY")
    secret_ok = _read_present(env, "KITE_API_SECRET")
    token_ok = _read_present(env, "KITE_ACCESS_TOKEN")
    can_connect = key_ok and secret_ok and not LIVE_TRADING_COMPILED

    if not key_ok or not secret_ok:
        return ZerodhaAuthStatus(
            api_key_present=key_ok,
            api_secret_present=secret_ok,
            access_token_present=token_ok,
            status="NOT_CONFIGURED",
            can_connect=False,
            message=(
                "Enter API key and API secret in Settings (auto-saved in this browser). "
                "Then click Connect. Market-data only; no orders."
            ),
        )

    if token_ok and probe and probe_session(env):
        return ZerodhaAuthStatus(
            api_key_present=key_ok,
            api_secret_present=secret_ok,
            access_token_present=token_ok,
            status="CONNECTED",
            can_connect=can_connect,
            message=(
                "Connected. Session is stored locally and will be reused until Zerodha "
                "expires the daily access token — then Connect again from Settings."
            ),
        )

    if token_ok:
        message = (
            "Access token is stored locally but Zerodha rejected or has not verified it. "
            "Click Connect to refresh the daily session."
        )
    else:
        message = (
            "Credentials are saved locally. Click Connect / Authorise once; "
            "you will stay connected on this machine until the daily token expires."
        )

    return ZerodhaAuthStatus(
        api_key_present=key_ok,
        api_secret_present=secret_ok,
        access_token_present=token_ok,
        status="DISCONNECTED",
        can_connect=can_connect,
        message=message,
    )


def complete_callback(
    *,
    request_token: str,
    status: str | None = None,
    environ: dict[str, str] | None = None,
    dotenv_path: Path | None = None,
    exchanger: Any | None = None,
) -> dict[str, Any]:
    """Handle OAuth callback: exchange token, persist, return public result."""
    if LIVE_TRADING_COMPILED:
        raise GrowConfigError("LIVE_TRADING_COMPILED")
    if str(status or "success").lower() not in {"success", ""}:
        raise GrowConfigError("AUTH_FAILED: login was not successful")
    exchange = exchanger or exchange_request_token
    access = exchange(request_token, environ=environ)
    path = persist_access_token(access, environ=environ, dotenv_path=dotenv_path)
    return {
        "ok": True,
        "status": "CONNECTED",
        "message": "Zerodha market-data authorised. Access token stored locally.",
        "dotenv_updated": True,
        "dotenv_name": path.name,
        "live_trading": False,
        "broker_order_path": False,
    }


__all__ = [
    "DEFAULT_REDIRECT_URI",
    "ZerodhaAuthStatus",
    "auth_status",
    "build_connect_redirect",
    "clear_access_token",
    "complete_callback",
    "exchange_request_token",
    "login_url",
    "persist_access_token",
    "persist_broker_credentials",
    "probe_session",
    "resolve_dotenv_path",
    "upsert_dotenv_value",
]
