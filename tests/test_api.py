from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from options_m.agents import HeartbeatAgent
from options_m.api import create_app
from options_m.config import Settings
from options_m.db import Database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """Client for an app with no database configured."""
    db = Database(Settings(database_url=None))
    app = create_app(db, [HeartbeatAgent("alpha")])
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
