"""HTTP surface: health probes, JSON API, and the admin dashboard.

Liveness and readiness are deliberately separate:

* ``/health`` answers "is this process alive?" and must stay cheap — the
  platform restarts the container when it fails. It never touches a dependency,
  and it never will.
* ``/ready`` answers "can it serve real work?" and does check dependencies.

The ``/api/*`` endpoints are what the dashboard polls. They are read-only in
this phase; the kill-switch control arrives with the dashboard proper.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse

from options_m import __version__
from options_m.agents import Agent
from options_m.db import Database
from options_m.mcp_client import AlpacaMcp
from options_m.store import Store

logger = logging.getLogger(__name__)

router = APIRouter()


def _state(request: Request) -> tuple[Database, list[Agent]]:
    return request.app.state.db, request.app.state.agents


def _mcp(request: Request) -> AlpacaMcp | None:
    mcp: AlpacaMcp | None = getattr(request.app.state, "mcp", None)
    return mcp


def _store(request: Request) -> Store | None:
    store: Store | None = getattr(request.app.state, "store", None)
    return store


@router.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Liveness probe. Intentionally does not touch dependencies."""
    return {"status": "ok", "version": __version__}


@router.get("/ready", include_in_schema=False)
async def ready(request: Request) -> Response:
    """Readiness probe. Reports dependency health."""
    db, _agents = _state(request)
    db_ok = await db.ping() if db.is_enabled else None
    mcp = _mcp(request)

    payload: dict[str, Any] = {
        "status": "ready" if db_ok is not False else "degraded",
        "database": {"enabled": db.is_enabled, "reachable": db_ok},
        "broker": {
            "enabled": mcp.is_enabled if mcp else False,
            "connected": mcp.is_connected if mcp else False,
            "dry_run": mcp.dry_run if mcp else None,
            # Supporting evidence, not proof: paper mode is guaranteed by
            # pinning the server's ALPACA_PAPER_TRADE, not by this flag.
            "paper_corroborated": mcp.paper_corroborated if mcp else None,
        },
    }
    code = status.HTTP_200_OK if db_ok is not False else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(payload, status_code=code)


@router.get("/api/status", include_in_schema=False)
async def api_status(request: Request) -> Response:
    """Live account and market state for the dashboard header."""
    _db, agents = _state(request)
    mcp = _mcp(request)
    store = _store(request)

    clock: Any = None
    account: Any = None
    broker_error: str | None = None
    if mcp is not None and mcp.is_enabled:
        try:
            clock = await mcp.get_clock()
            account = await mcp.get_account_info()
        except Exception as exc:
            # The dashboard must render with the broker down. It says so
            # explicitly rather than showing a plausible-looking zero.
            broker_error = f"{type(exc).__name__}: {exc}"
            logger.warning("status broker read failed", exc_info=True)

    equity = await store.recent_equity(limit=120) if store else []

    return JSONResponse(
        jsonable(
            {
                "version": __version__,
                "clock": clock,
                "account": account,
                "broker": {
                    "enabled": mcp.is_enabled if mcp else False,
                    "connected": mcp.is_connected if mcp else False,
                    "dry_run": mcp.dry_run if mcp else None,
                    "paper_corroborated": mcp.paper_corroborated if mcp else None,
                    "options_trading_level": mcp.options_trading_level if mcp else None,
                    "error": broker_error,
                },
                "agents": [agent.name for agent in agents],
                "persistent": store.is_persistent if store else False,
                "equity_tail": list(reversed(equity)),
            }
        )
    )


@router.get("/api/agent-runs", include_in_schema=False)
async def api_agent_runs(request: Request, limit: int = 50) -> Response:
    store = _store(request)
    runs = await store.recent_agent_runs(limit=min(limit, 500)) if store else []
    return JSONResponse(jsonable({"runs": runs}))


@router.get("/api/candidates", include_in_schema=False)
async def api_candidates(request: Request, limit: int = 20) -> Response:
    store = _store(request)
    rows = await store.recent_candidates(limit=min(limit, 200)) if store else []
    return JSONResponse(jsonable({"candidates": rows}))


def jsonable(value: Any) -> Any:
    """Make datetimes and Decimals safe for JSONResponse.

    Decimals become floats only at the boundary, on the way out. Inside the
    system money stays exact.
    """
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if type(value).__name__ == "Decimal":
        return float(value)
    return value


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    """Minimal admin dashboard shell. Replaced by the real one in phase 4."""
    _db, agents = _state(request)
    rows = "".join(f"<li>{agent.name}</li>" for agent in agents) or "<li>none registered</li>"
    return HTMLResponse(
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>options-m admin</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:40rem}</style>"
        "</head><body>"
        "<h1>options-m</h1>"
        f"<p>version {__version__}</p>"
        "<h2>Agents</h2>"
        f"<ul>{rows}</ul>"
        '<p><a href="/api/status">status</a> &middot; '
        '<a href="/api/agent-runs">agent runs</a> &middot; '
        '<a href="/ready">readiness</a></p>'
        "</body></html>"
    )


def create_app(
    db: Database,
    agents: list[Agent],
    *,
    mcp: AlpacaMcp | None = None,
    store: Store | None = None,
) -> FastAPI:
    """Build the ASGI application with its dependencies attached."""
    app = FastAPI(title="options-m admin", version=__version__, docs_url=None, redoc_url=None)
    app.state.db = db
    app.state.agents = agents
    app.state.mcp = mcp
    app.state.store = store
    app.include_router(router)
    return app
