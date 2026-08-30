"""Tests for PositionManagerAgent.

Scoped to what this agent now does: own the ``positions`` cache and enrich each
row with proposal metadata + pnl_pct. Exit decisions belong to StrategistAgent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from options_m.agents.position_manager import (
    PositionManagerAgent,
    _compute_pnl_pct,
    _enrich_from_orders,
)
from options_m.config import Settings
from options_m.db import Database
from options_m.store import Store

# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------


class _FakeMcp:
    def __init__(
        self,
        *,
        positions: list[dict[str, Any]] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self._positions = positions if positions is not None else []
        self._fail_on = fail_on
        self.called: list[str] = []

    async def get_all_positions(self) -> list[dict[str, Any]]:
        self.called.append("get_all_positions")
        if self._fail_on == "get_all_positions":
            msg = "get_all_positions is down"
            raise RuntimeError(msg)
        return self._positions


def _agent(mcp: Any, **settings_kwargs: Any) -> tuple[PositionManagerAgent, Store]:
    settings = Settings(database_url=None, **settings_kwargs)
    store = Store(Database(settings))
    return PositionManagerAgent(settings, mcp, store), store


# ---------------------------------------------------------------------------
# Cache ownership
# ---------------------------------------------------------------------------


async def test_uses_its_own_cadence_not_the_global_default() -> None:
    settings = Settings(database_url=None, position_manager_interval_seconds=45.0)
    store = Store(Database(settings))
    agent = PositionManagerAgent(settings, _FakeMcp(), store)  # type: ignore[arg-type]
    assert agent.interval_seconds == 45.0


async def test_no_open_positions_leaves_the_cache_empty() -> None:
    agent, store = _agent(_FakeMcp(positions=[]))
    await agent.step()
    assert await store.get_cached_positions() == []


async def test_a_single_leg_position_is_cached_under_its_underlying() -> None:
    mcp = _FakeMcp(
        positions=[{"symbol": "AAPL250321C00150000", "qty": "1", "unrealized_pl": "12.50"}]
    )
    agent, store = _agent(mcp)
    await agent.step()

    cached = {row["symbol"]: row["payload"] for row in await store.get_cached_positions()}
    assert set(cached) == {"AAPL"}
    assert cached["AAPL"]["legs"][0]["symbol"] == "AAPL250321C00150000"


async def test_multiple_legs_of_one_structure_group_under_one_underlying() -> None:
    mcp = _FakeMcp(
        positions=[
            {"symbol": "SPY250321P00500000", "side": "short"},
            {"symbol": "SPY250321P00490000", "side": "long"},
            {"symbol": "SPY250321C00560000", "side": "short"},
            {"symbol": "SPY250321C00570000", "side": "long"},
        ]
    )
    agent, store = _agent(mcp)
    await agent.step()

    cached = await store.get_cached_positions()
    assert len(cached) == 1
    assert cached[0]["symbol"] == "SPY"
    assert len(cached[0]["payload"]["legs"]) == 4


async def test_a_closed_position_is_dropped_from_the_cache() -> None:
    settings = Settings(database_url=None)
    store = Store(Database(settings))
    mcp1 = _FakeMcp(positions=[{"symbol": "AAPL250321C00150000"}])
    await PositionManagerAgent(settings, mcp1, store).step()  # type: ignore[arg-type]
    assert {row["symbol"] for row in await store.get_cached_positions()} == {"AAPL"}

    await PositionManagerAgent(settings, _FakeMcp(positions=[]), store).step()  # type: ignore[arg-type]
    assert await store.get_cached_positions() == []


async def test_an_unrecognisable_symbol_falls_back_to_itself() -> None:
    agent, store = _agent(_FakeMcp(positions=[{"symbol": "WEIRD-SHAPE"}]))
    await agent.step()
    assert {row["symbol"] for row in await store.get_cached_positions()} == {"WEIRD-SHAPE"}


async def test_a_broker_failure_propagates_to_the_supervisor() -> None:
    agent, store = _agent(_FakeMcp(fail_on="get_all_positions"))
    with pytest.raises(RuntimeError, match="get_all_positions is down"):
        await agent.step()
    run = (await store.recent_agent_runs())[0]
    assert run["ok"] is False


async def test_a_successful_step_is_recorded_with_its_detail() -> None:
    agent, store = _agent(_FakeMcp(positions=[{"symbol": "AAPL250321C00150000"}]))
    await agent.step()
    run = (await store.recent_agent_runs())[0]
    assert run["agent"] == "position_manager"
    assert run["ok"] is True
    assert run["detail"]["open_underlyings"] == 1


# ---------------------------------------------------------------------------
# Enrichment and pnl_pct
# ---------------------------------------------------------------------------


async def test_pnl_pct_is_written_to_the_payload() -> None:
    """pnl_pct is pre-computed so StrategistAgent can read it directly."""
    mcp = _FakeMcp(
        positions=[
            {
                "symbol": "AAPL991231C00150000",
                "unrealized_pl": "500.0",
                "market_value": "1500.0",
            }
        ]
    )
    agent, store = _agent(mcp)
    await agent.step()

    cached = {row["symbol"]: row["payload"] for row in await store.get_cached_positions()}
    # entry_value = 1500 - 500 = 1000, pnl_pct = 500/1000 = 0.50
    assert cached["AAPL"]["pnl_pct"] == pytest.approx(0.50)


async def test_pnl_pct_is_none_when_no_market_data() -> None:
    mcp = _FakeMcp(positions=[{"symbol": "AAPL991231C00150000"}])
    agent, store = _agent(mcp)
    await agent.step()
    cached = {row["symbol"]: row["payload"] for row in await store.get_cached_positions()}
    assert cached["AAPL"]["pnl_pct"] is None


def test_compute_pnl_pct_profit() -> None:
    assert _compute_pnl_pct({"unrealized_pl": 500.0, "market_value": 1500.0}) == pytest.approx(0.50)


def test_compute_pnl_pct_loss() -> None:
    # market_value=500, unrealized_pl=-500, entry_value=1000, pnl=-50%
    result = _compute_pnl_pct({"unrealized_pl": -500.0, "market_value": 500.0})
    assert result == pytest.approx(-0.50)


def test_compute_pnl_pct_returns_none_when_missing() -> None:
    assert _compute_pnl_pct({}) is None
    assert _compute_pnl_pct({"unrealized_pl": 100.0}) is None


def test_enrich_from_orders_matches_entry_order() -> None:
    payload: dict[str, Any] = {}
    legs = [{"symbol": "AAPL250321C00150000"}]
    orders = [
        {
            "client_order_id": "om-42",
            "status": "filled",
            "filled_avg_price": 1.75,
            "submitted_at": datetime(2026, 1, 15, tzinfo=UTC),
            "request": {"symbol": "AAPL250321C00150000", "side": "buy"},
        }
    ]
    _enrich_from_orders(payload, legs, orders)
    assert payload["proposal_id"] == 42
    assert payload["entry_price"] == 1.75


def test_enrich_from_orders_skips_close_orders() -> None:
    """Close orders (omc- prefix) share OCC symbols but must not be used as entry."""
    payload: dict[str, Any] = {}
    orders = [
        {
            "client_order_id": "omc-42",
            "status": "filled",
            "filled_avg_price": 1.75,
            "submitted_at": datetime(2026, 1, 15, tzinfo=UTC),
            "request": {"symbol": "AAPL991231C00150000", "side": "sell"},
        }
    ]
    _enrich_from_orders(payload, [{"symbol": "AAPL991231C00150000"}], orders)
    assert "proposal_id" not in payload


def test_enrich_from_orders_no_match_leaves_payload_unchanged() -> None:
    payload: dict[str, Any] = {}
    _enrich_from_orders(payload, [{"symbol": "AAPL250321C00150000"}], [])
    assert payload == {}
