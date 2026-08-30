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


# ---- dashboard API: positions/portfolio/proposals/risk-events -----------


async def test_positions_renders_without_a_broker(client: httpx.AsyncClient) -> None:
    """Same honesty rule as /api/status: no broker, no fabricated rows."""
    response = await client.get("/api/positions")

    assert response.status_code == 200
    body = response.json()
    assert body["positions"] == []
    assert body["broker"]["enabled"] is False


async def test_portfolio_renders_without_a_broker(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/portfolio")

    assert response.status_code == 200
    body = response.json()
    assert body["account"] is None
    assert body["portfolio_history"] is None
    assert body["equity_curve_tail"] == []


async def test_proposals_list_is_empty_without_history(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/proposals")

    assert response.status_code == 200
    assert response.json() == {"proposals": []}


async def test_proposal_detail_reports_the_full_row_and_its_orders() -> None:
    db = Database(Settings(database_url=None))
    store = Store(db)
    app = create_app(db, [], mcp=None, store=store)
    proposal_id = await store.save_proposal(
        underlying="SPY", intent={"direction": "long"}, evidence={}
    )
    await store.record_order(
        proposal_id=proposal_id, client_order_id="om-1", status="submitted", request={}
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/proposals/{proposal_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["proposal"]["underlying"] == "SPY"
    assert body["orders"][0]["client_order_id"] == "om-1"


async def test_proposal_detail_404s_for_an_unknown_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/proposals/999999")

    assert response.status_code == 404


async def test_risk_events_endpoint_is_empty_without_history(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/risk-events")

    assert response.status_code == 200
    assert response.json() == {"risk_events": []}


# ---- dashboard API: auth and CORS ----------------------------------------


async def test_dashboard_routes_are_open_when_no_admin_token_is_configured(
    client: httpx.AsyncClient,
) -> None:
    """Unset ADMIN_TOKEN means local/dev use needs no setup."""
    response = await client.get("/api/positions")

    assert response.status_code == 200


async def test_dashboard_routes_require_the_bearer_token_when_configured() -> None:
    db = Database(Settings(database_url=None))
    settings = Settings(admin_token="secret")  # noqa: S106 - test fixture, not a real credential
    app = create_app(db, [], mcp=None, store=Store(db), settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthenticated = await client.get("/api/positions")
        wrong_token = await client.get(
            "/api/positions", headers={"Authorization": "Bearer nope"}
        )
        right_token = await client.get(
            "/api/positions", headers={"Authorization": "Bearer secret"}
        )

    assert unauthenticated.status_code == 401
    assert wrong_token.status_code == 401
    assert right_token.status_code == 200


async def test_the_pre_existing_status_route_stays_unauthenticated_even_with_a_token() -> None:
    """The original /api/status contract must not change under this work."""
    db = Database(Settings(database_url=None))
    settings = Settings(admin_token="secret")  # noqa: S106 - test fixture, not a real credential
    app = create_app(db, [], mcp=None, store=Store(db), settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/status")

    assert response.status_code == 200


# ---- dashboard API: chat --------------------------------------------------


async def test_chat_reports_when_the_llm_is_not_configured(client: httpx.AsyncClient) -> None:
    """Featherless credentials are unset in the test fixture on purpose."""
    response = await client.post("/api/chat", json={"question": "what's my equity?"})

    assert response.status_code == 200
    body = response.json()
    assert "llm_unconfigured" in body["warnings"]


# ---- admin: kill switch ---------------------------------------------------


async def test_kill_switch_starts_released(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/kill")

    assert response.status_code == 200
    assert response.json() == {
        "engaged": False,
        "reason": None,
        "updated_at": None,
        "env_forced": False,
        "effective": False,
    }


async def test_engaging_the_kill_switch_needs_no_reason(client: httpx.AsyncClient) -> None:
    """Halting must never be gated behind a form field."""
    response = await client.post("/admin/kill", json={"engaged": True})

    assert response.status_code == 200
    body = response.json()
    assert body["engaged"] is True
    assert body["effective"] is True


async def test_releasing_the_kill_switch_requires_a_reason(client: httpx.AsyncClient) -> None:
    await client.post("/admin/kill", json={"engaged": True, "reason": "spread blowout"})

    refused = await client.post("/admin/kill", json={"engaged": False})
    blank = await client.post("/admin/kill", json={"engaged": False, "reason": "   "})

    assert refused.status_code == 422
    assert blank.status_code == 422, "whitespace is not a reason"
    assert (await client.get("/admin/kill")).json()["engaged"] is True


async def test_releasing_with_a_reason_succeeds_and_records_it(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/admin/kill", json={"engaged": True, "reason": "spread blowout"})

    response = await client.post(
        "/admin/kill", json={"engaged": False, "reason": "spreads normal again"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["engaged"] is False
    assert body["reason"] == "spreads normal again"


async def test_kill_switch_writes_an_audit_event(client: httpx.AsyncClient) -> None:
    """A halt should be visible on the feed next to the rejections it causes."""
    await client.post("/admin/kill", json={"engaged": True, "reason": "manual halt"})

    events = (await client.get("/api/risk-events")).json()["risk_events"]

    assert events[0]["rule"] == "kill_switch_engaged"
    assert events[0]["detail"] == {"reason": "manual halt", "source": "admin_api"}


async def test_env_forced_kill_switch_is_reported_and_cannot_be_released() -> None:
    """``KILL_SWITCH=true`` outranks the stored flag, so the UI must not
    claim trading resumed when the agents will still refuse."""
    db = Database(Settings(database_url=None))
    settings = Settings(kill_switch=True)
    app = create_app(db, [], mcp=None, store=Store(db), settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        released = await client.post(
            "/admin/kill", json={"engaged": False, "reason": "trying to resume"}
        )

    body = released.json()
    assert body["engaged"] is False, "the stored flag did release"
    assert body["env_forced"] is True
    assert body["effective"] is True, "but the agents still see it engaged"


async def test_kill_switch_routes_require_the_admin_token() -> None:
    db = Database(Settings(database_url=None))
    settings = Settings(admin_token="secret")  # noqa: S106 - test fixture, not a real credential
    app = create_app(db, [], mcp=None, store=Store(db), settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthenticated = await client.post("/admin/kill", json={"engaged": True})
        authenticated = await client.post(
            "/admin/kill",
            json={"engaged": True},
            headers={"Authorization": "Bearer secret"},
        )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
