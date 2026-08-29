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

import dataclasses
import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from options_m import __version__, chat
from options_m.agents import Agent
from options_m.config import Settings
from options_m.db import Database
from options_m.llm import FeatherlessLlm
from options_m.mcp_client import AlpacaMcp
from options_m.store import Store

logger = logging.getLogger(__name__)

router = APIRouter()


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def require_admin_token(
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    """Guard for the dashboard-facing routes below.

    A single shared bearer token, deliberately simpler than a real user-auth
    system: this protects a judge-facing demo, not a multi-tenant product. If
    no token is configured the routes stay open — that only matters for
    local/dev use, since ``ADMIN_TOKEN`` must be set wherever this is
    reachable from the public internet.
    """
    settings = _settings(request)
    if not settings.admin_token:
        logger.warning("ADMIN_TOKEN is not set; the dashboard API routes are unauthenticated")
        return
    if authorization != f"Bearer {settings.admin_token}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


# Dashboard-facing routes, added alongside the pre-existing unauthenticated
# router below. Kept separate so the original /api/status, /api/agent-runs,
# /api/candidates contract is untouched.
admin_router = APIRouter(prefix="/api", dependencies=[Depends(require_admin_token)])


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
    settings = _settings(request)

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
                # The clock above says closed while the agents deliberately act
                # on the last session. The dashboard must be able to say so
                # rather than present a replayed run as a live one.
                "replay_last_session": settings.replay_last_session,
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


@admin_router.get("/positions", include_in_schema=False)
async def api_positions(request: Request) -> Response:
    """Live open positions, enriched with greeks/IV for option legs.

    No Postgres table backs this yet (positions_history is a later phase), so
    it is entirely a live read through MCP, refreshed on every poll.
    """
    mcp = _mcp(request)
    positions: list[dict[str, Any]] = []
    broker_error: str | None = None

    if mcp is not None and mcp.is_enabled:
        try:
            raw_positions = await mcp.get_all_positions()
        except Exception as exc:
            broker_error = f"{type(exc).__name__}: {exc}"
            logger.warning("positions broker read failed", exc_info=True)
            raw_positions = []

        option_symbols = [
            str(row["symbol"])
            for row in raw_positions
            if row.get("asset_class") == "us_option" and row.get("symbol")
        ]
        snapshots: dict[str, dict[str, Any]] = {}
        snapshot_error: str | None = None
        if option_symbols:
            try:
                snapshots = await mcp.get_option_snapshot(option_symbols)
            except Exception as exc:
                snapshot_error = f"{type(exc).__name__}: {exc}"
                logger.warning("option snapshot read failed", exc_info=True)

        for row in raw_positions:
            symbol = row.get("symbol")
            is_option = row.get("asset_class") == "us_option"
            snapshot = snapshots.get(symbol) if is_option and isinstance(symbol, str) else None
            row_error: str | None = None
            if is_option:
                row_error = snapshot_error
                if row_error is None and snapshot is None:
                    row_error = "snapshot unavailable"
            positions.append({**row, "snapshot": snapshot, "snapshot_error": row_error})

    return JSONResponse(
        jsonable(
            {
                "positions": positions,
                "broker": {
                    "enabled": mcp.is_enabled if mcp else False,
                    "connected": mcp.is_connected if mcp else False,
                    "error": broker_error,
                },
            }
        )
    )


@admin_router.get("/portfolio", include_in_schema=False)
async def api_portfolio(request: Request, period: str = "1M", timeframe: str = "1D") -> Response:
    """Account snapshot, Alpaca's own portfolio-history series, and our own
    polled equity-curve tail, side by side.
    """
    mcp = _mcp(request)
    store = _store(request)

    account: dict[str, Any] | None = None
    portfolio_history: dict[str, Any] | None = None
    broker_error: str | None = None
    if mcp is not None and mcp.is_enabled:
        try:
            account = await mcp.get_account_info()
            portfolio_history = await mcp.get_portfolio_history(period=period, timeframe=timeframe)
        except Exception as exc:
            broker_error = f"{type(exc).__name__}: {exc}"
            logger.warning("portfolio broker read failed", exc_info=True)

    equity_tail = await store.recent_equity(limit=200) if store else []

    return JSONResponse(
        jsonable(
            {
                "account": account,
                "portfolio_history": portfolio_history,
                "equity_curve_tail": list(reversed(equity_tail)),
                "broker_error": broker_error,
            }
        )
    )


@admin_router.get("/proposals", include_in_schema=False)
async def api_proposals(
    request: Request, limit: int = 50, status_filter: str | None = None
) -> Response:
    store = _store(request)
    rows = (
        await store.recent_proposals(limit=min(limit, 500), status=status_filter)
        if store
        else []
    )
    return JSONResponse(jsonable({"proposals": rows}))


@admin_router.get("/proposals/{proposal_id}", include_in_schema=False)
async def api_proposal_detail(request: Request, proposal_id: int) -> Response:
    store = _store(request)
    if store is None:
        return JSONResponse(
            {"detail": "store not configured"}, status_code=status.HTTP_404_NOT_FOUND
        )
    proposal = await store.get_proposal(proposal_id)
    if proposal is None:
        return JSONResponse({"detail": "not found"}, status_code=status.HTTP_404_NOT_FOUND)
    orders = await store.orders_for_proposal(proposal_id)
    return JSONResponse(jsonable({"proposal": proposal, "orders": orders}))


@admin_router.get("/risk-events", include_in_schema=False)
async def api_risk_events(request: Request, limit: int = 50) -> Response:
    store = _store(request)
    rows = await store.recent_risk_events(limit=min(limit, 500)) if store else []
    return JSONResponse(jsonable({"risk_events": rows}))


class ChatRequest(BaseModel):
    question: str


@admin_router.post("/chat", include_in_schema=False)
async def api_chat(request: Request, body: ChatRequest) -> Response:
    """Read-only Q&A over the account and the agent's own decision history.

    Always 200, even on failure: a chat panel that 500s mid-demo is worse
    than one that plainly says it could not reach the broker or the model.
    """
    settings = _settings(request)
    mcp = _mcp(request)
    store = _store(request)
    llm = FeatherlessLlm(
        api_key=settings.featherless_api_key,
        base_url=settings.featherless_base_url,
        model=settings.featherless_chat_model,
        timeout_seconds=settings.chat_timeout_seconds,
    )
    answer = await chat.answer_question(
        body.question,
        mcp=mcp,
        store=store,
        llm=llm,
        max_tool_calls=settings.chat_max_tool_calls,
    )
    return JSONResponse(jsonable(dataclasses.asdict(answer)))


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
    settings: Settings | None = None,
) -> FastAPI:
    """Build the ASGI application with its dependencies attached."""
    app = FastAPI(title="options-m admin", version=__version__, docs_url=None, redoc_url=None)
    app.state.db = db
    app.state.agents = agents
    app.state.mcp = mcp
    app.state.store = store
    app.state.settings = settings if settings is not None else Settings()

    cors_origins = app.state.settings.cors_origins
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
        )

    app.include_router(router)
    app.include_router(admin_router)
    return app
