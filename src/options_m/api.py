"""HTTP surface: health probes and the admin dashboard.

Liveness and readiness are deliberately separate:

* ``/health`` answers "is this process alive?" and must stay cheap — the
  platform restarts the container when it fails.
* ``/ready`` answers "can it serve real work?" and does check dependencies.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse

from options_m import __version__
from options_m.agents import Agent
from options_m.db import Database

logger = logging.getLogger(__name__)

router = APIRouter()


def _state(request: Request) -> tuple[Database, list[Agent]]:
    return request.app.state.db, request.app.state.agents


@router.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Liveness probe. Intentionally does not touch dependencies."""
    return {"status": "ok", "version": __version__}


@router.get("/ready", include_in_schema=False)
async def ready(request: Request) -> Response:
    """Readiness probe. Reports dependency health."""
    db, _agents = _state(request)
    db_ok = await db.ping() if db.is_enabled else None

    payload: dict[str, Any] = {
        "status": "ready" if db_ok is not False else "degraded",
        "database": {"enabled": db.is_enabled, "reachable": db_ok},
    }
    code = status.HTTP_200_OK if db_ok is not False else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(payload, status_code=code)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    """Minimal admin dashboard shell."""
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
        '<p><a href="/ready">readiness</a></p>'
        "</body></html>"
    )


def create_app(db: Database, agents: list[Agent]) -> FastAPI:
    """Build the ASGI application with its dependencies attached."""
    app = FastAPI(title="options-m admin", version=__version__, docs_url=None, redoc_url=None)
    app.state.db = db
    app.state.agents = agents
    app.include_router(router)
    return app
