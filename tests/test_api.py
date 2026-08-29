from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from options_m.api import create_app
from options_m.config import Settings
from options_m.db import Database
from options_m.store import Store


class _StubAgent:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def step(self) -> None:  # pragma: no cover - never driven here
        return None


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """Client for an app with neither a database nor a broker configured."""
    db = Database(Settings(database_url=None))
    app = create_app(db, [_StubAgent("alpha")], mcp=None, store=Store(db))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def test_health_is_ok_without_dependencies(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_ready_reports_database_disabled(client: httpx.AsyncClient) -> None:
    response = await client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == {"enabled": False, "reachable": None}
    assert body["broker"]["enabled"] is False


async def test_ready_is_unavailable_when_database_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(Settings(database_url="postgresql://unused/db"))

    async def _failing_ping() -> bool:
        return False

    monkeypatch.setattr(db, "ping", _failing_ping)
    app = create_app(db, [])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


async def test_dashboard_lists_registered_agents(client: httpx.AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert "alpha" in response.text


async def test_status_renders_without_a_broker_or_database(client: httpx.AsyncClient) -> None:
    """The dashboard must degrade honestly rather than inventing numbers."""
    response = await client.get("/api/status")

    assert response.status_code == 200
    body = response.json()
    assert body["clock"] is None
    assert body["account"] is None
    assert body["broker"]["enabled"] is False
    assert body["persistent"] is False
    assert body["equity_tail"] == []


async def test_agent_runs_endpoint_is_empty_without_history(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/agent-runs")

    assert response.status_code == 200
    assert response.json() == {"runs": []}
