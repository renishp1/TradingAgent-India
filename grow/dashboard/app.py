"""Paper dashboard FastAPI app.

Serves a static trading-terminal UI and JSON APIs.
Paper risk limits are editable; Zerodha Connect is market-data OAuth only.
Does not place orders or enable live trading.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from grow.dashboard.service import DashboardService, build_service_from_environ
from grow.errors import GrowConfigError
from grow.execution.lock import LIVE_TRADING_COMPILED

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _public_auth_error(exc: BaseException) -> str:
    if isinstance(exc, GrowConfigError):
        text = str(exc)
        # Never echo tokens / secrets if a bug leaks them into the message.
        lowered = text.lower()
        if "token" in lowered and "missing" not in lowered and "auth_" not in lowered:
            return "AUTH_FAILED"
        return text
    return "AUTH_FAILED"


def _callback_result_page(*, ok: bool, message: str) -> str:
    status = "Connected" if ok else "Connection failed"
    tone = "#3dd68c" if ok else "#ff6b6b"
    safe = (
        str(message)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Zerodha Connect · TradingAgent-India</title>
  <style>
    body {{ margin:0; font-family:Segoe UI,Helvetica Neue,sans-serif;
      background:#0b0f14; color:#e7eef7; display:flex; min-height:100vh;
      align-items:center; justify-content:center; }}
    .card {{ max-width:28rem; padding:1.5rem 1.75rem; border:1px solid #243041;
      border-radius:8px; background:#121821; }}
    h1 {{ margin:0 0 .5rem; font-size:1.2rem; color:{tone}; }}
    p {{ margin:.4rem 0; color:#8b9bb0; line-height:1.45; }}
    a {{ color:#5b9fd4; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{status}</h1>
    <p>{safe}</p>
    <p>Market-data only · Live trading disabled · Broker orders disabled</p>
    <p><a href="/?view=settings">Back to Settings</a></p>
    <script>
      try {{
        if (window.opener && !window.opener.closed) {{
          window.opener.postMessage({{ type: "zerodha-auth", ok: {str(ok).lower()} }}, window.location.origin);
        }}
      }} catch (e) {{}}
      setTimeout(function () {{ window.location.replace("/?view=settings&zerodha={'ok' if ok else 'err'}"); }}, 1200);
    </script>
  </div>
</body>
</html>"""


def create_app(service: DashboardService | None = None) -> FastAPI:
    svc = service or build_service_from_environ()

    app = FastAPI(
        title="TradingAgent-India Dashboard",
        description="Paper-trading console (Phase 1). Zerodha Connect = market-data auth only.",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )
    app.state.service = svc

    @app.get("/api/health")
    def api_health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "grow-dashboard",
            "phase": 1,
            "mode": "paper",
            "read_only": True,
            "live_trading": False,
            "broker_order_path": False,
        }

    @app.get("/api/dashboard")
    def api_dashboard(request: Request) -> dict[str, Any]:
        return request.app.state.service.dashboard()

    @app.get("/api/config")
    def api_config(request: Request) -> dict[str, Any]:
        return request.app.state.service.config_view()

    @app.get("/api/positions")
    def api_positions(request: Request) -> dict[str, Any]:
        return request.app.state.service.positions_view()

    @app.get("/api/risk")
    def api_risk(request: Request) -> dict[str, Any]:
        return request.app.state.service.risk_view()

    @app.get("/api/risk/config")
    def api_risk_config_get(request: Request) -> dict[str, Any]:
        return request.app.state.service.risk_config_view()

    @app.post("/api/risk/config")
    async def api_risk_config_set(request: Request) -> JSONResponse:
        """Update paper capital / risk limits. Never enables live trading."""
        if LIVE_TRADING_COMPILED:
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": "LIVE_TRADING_COMPILED", "live_trading": False},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False, "error": "INVALID_JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"ok": False, "error": "INVALID_JSON"})
        try:
            result = request.app.state.service.update_paper_risk_config(body)
            return JSONResponse(result)
        except GrowConfigError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": str(exc),
                    "live_trading": False,
                    "broker_order_path": False,
                },
            )

    @app.post("/api/risk/config/reset")
    def api_risk_config_reset(request: Request) -> JSONResponse:
        if LIVE_TRADING_COMPILED:
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": "LIVE_TRADING_COMPILED", "live_trading": False},
            )
        try:
            result = request.app.state.service.reset_paper_risk_config()
            return JSONResponse(result)
        except GrowConfigError as exc:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": str(exc), "live_trading": False},
            )

    @app.get("/api/agents")
    def api_agents(request: Request) -> dict[str, Any]:
        return request.app.state.service.agents_view()

    @app.get("/api/safety")
    def api_safety(request: Request) -> dict[str, Any]:
        return request.app.state.service.safety()

    @app.get("/api/market")
    def api_market(request: Request) -> dict[str, Any]:
        return request.app.state.service.market_status()

    @app.get("/api/option-chain")
    def api_option_chain(request: Request) -> dict[str, Any]:
        return request.app.state.service.option_chain_view()

    @app.get("/api/signals")
    def api_signals(request: Request) -> dict[str, Any]:
        return request.app.state.service.signals_view()

    @app.get("/api/research")
    def api_research(request: Request) -> dict[str, Any]:
        return request.app.state.service.research_view()

    @app.get("/api/settings")
    def api_settings(request: Request) -> dict[str, Any]:
        # Default to verified status (same TTL path as market/system) so Settings
        # cannot flip CONNECTED → DISCONNECTED on unprobed refresh.
        probe_raw = str(request.query_params.get("probe") or "1").strip().lower()
        probe = probe_raw not in {"0", "false", "no"}
        return request.app.state.service.settings_view(probe_zerodha=probe)

    @app.get("/api/zerodha/status")
    def api_zerodha_status(request: Request) -> dict[str, Any]:
        probe = str(request.query_params.get("probe") or "1").strip().lower()
        # Default probe=1 so status matches dashboard market/system verified path.
        do_probe = probe not in {"0", "false", "no"}
        force = str(request.query_params.get("force") or "").strip() in {"1", "true", "yes"}
        return request.app.state.service._zerodha_public(probe=do_probe, force=force)

    @app.get("/api/zerodha/connect", response_model=None)
    def api_zerodha_connect():
        """Start Kite Connect login (market-data). Redirects to Zerodha."""
        if LIVE_TRADING_COMPILED:
            return JSONResponse(
                status_code=403,
                content={"error": "LIVE_TRADING_COMPILED", "live_trading": False},
            )
        try:
            from grow.dashboard.zerodha_auth import build_connect_redirect

            url = build_connect_redirect()
        except GrowConfigError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": _public_auth_error(exc),
                    "live_trading": False,
                    "broker_order_path": False,
                },
            )
        return RedirectResponse(url=url, status_code=302)

    @app.get("/api/zerodha/disconnect")
    def api_zerodha_disconnect() -> dict[str, Any]:
        """Clear stored access token (market-data session only)."""
        if LIVE_TRADING_COMPILED:
            return {"ok": False, "error": "LIVE_TRADING_COMPILED"}
        from grow.dashboard.zerodha_auth import clear_access_token

        clear_access_token()
        return {
            "ok": True,
            "status": "DISCONNECTED",
            "message": "Zerodha access token cleared locally.",
            "live_trading": False,
            "broker_order_path": False,
        }

    @app.post("/api/zerodha/credentials")
    async def api_zerodha_credentials(request: Request) -> JSONResponse:
        """Save API key + secret to local .env (machine storage). Never echoes secrets."""
        if LIVE_TRADING_COMPILED:
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": "LIVE_TRADING_COMPILED", "live_trading": False},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False, "error": "INVALID_JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"ok": False, "error": "INVALID_JSON"})
        # Reject any attempt to flip trading flags via this endpoint.
        for banned in ("live_trading", "live_trading_enabled", "place_order", "order"):
            if banned in body:
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error": "REFUSED",
                        "live_trading": False,
                        "broker_order_path": False,
                    },
                )
        api_key = body.get("api_key")
        api_secret = body.get("api_secret")
        if api_key is None and api_secret is None:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "AUTH_MISSING: provide api_key and/or api_secret"},
            )
        try:
            from grow.dashboard.zerodha_auth import persist_broker_credentials

            result = persist_broker_credentials(
                api_key=None if api_key is None else str(api_key),
                api_secret=None if api_secret is None else str(api_secret),
            )
            return JSONResponse(result)
        except GrowConfigError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": _public_auth_error(exc),
                    "live_trading": False,
                    "broker_order_path": False,
                },
            )

    @app.get("/auth/zerodha/callback")
    def auth_zerodha_callback(request: Request) -> HTMLResponse:
        """OAuth redirect target registered in the Kite Connect app."""
        if LIVE_TRADING_COMPILED:
            return HTMLResponse(
                _callback_result_page(ok=False, message="Live trading build refuses auth."),
                status_code=403,
            )
        params = request.query_params
        status = str(params.get("status") or "")
        request_token = str(params.get("request_token") or "")
        try:
            from grow.dashboard.zerodha_auth import complete_callback

            result = complete_callback(request_token=request_token, status=status)
            return HTMLResponse(
                _callback_result_page(ok=True, message=str(result["message"])),
                status_code=200,
            )
        except GrowConfigError as exc:
            return HTMLResponse(
                _callback_result_page(ok=False, message=_public_auth_error(exc)),
                status_code=400,
            )
        except Exception:
            return HTMLResponse(
                _callback_result_page(ok=False, message="AUTH_FAILED"),
                status_code=400,
            )

    @app.api_route("/api/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
    def api_mutations_forbidden(path: str) -> JSONResponse:
        return JSONResponse(
            status_code=405,
            content={
                "error": "Dashboard is read-only",
                "path": path,
                "live_trading": False,
                "broker_order_path": False,
                "can_enable_live_trading": False,
            },
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def strip_secret_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Grow-Mode"] = "paper"
        response.headers["X-Grow-Live-Trading"] = "disabled"
        return response

    return app


def get_service(app: FastAPI) -> DashboardService:
    return app.state.service


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "STATIC_DIR",
    "create_app",
    "get_service",
]
