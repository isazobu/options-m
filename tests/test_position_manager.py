"""Tests for PositionManagerAgent.

Scoped to what this module actually does today: own the local ``positions``
cache. See the module docstring for why the exit-rule engine is not here yet.
"""

from __future__ import annotations

from typing import Any

import pytest

from options_m.config import Settings
from options_m.db import Database
from options_m.store import Store
from options_m.agents.position_manager import PositionManagerAgent


class _FakeMcp:
    def __init__(
        self, *, positions: list[dict[str, Any]] | None = None, fail_on: str | None = None
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


def _agent(mcp: Any) -> tuple[PositionManagerAgent, Store]:
    settings = Settings(database_url=None)
    store = Store(Database(settings))
    return PositionManagerAgent(settings, mcp, store), store


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
    """A 4-leg iron condor is 4 rows from get_all_positions but one row here --
    this is what lets the per-underlying cap read a single cache row."""
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
    """The cache mirrors reality, not history -- a position that disappears
    from get_all_positions must disappear from here too, not linger."""
    settings = Settings(database_url=None)
    store = Store(Database(settings))
    opening_mcp = _FakeMcp(positions=[{"symbol": "AAPL250321C00150000"}])
    first_tick = PositionManagerAgent(settings, opening_mcp, store)  # type: ignore[arg-type]
    await first_tick.step()
    assert {row["symbol"] for row in await store.get_cached_positions()} == {"AAPL"}

    # Same store, next tick: the position closed since the last poll.
    closing_mcp = _FakeMcp(positions=[])
    second_tick = PositionManagerAgent(settings, closing_mcp, store)  # type: ignore[arg-type]
    await second_tick.step()

    assert await store.get_cached_positions() == []


async def test_an_unrecognisable_symbol_falls_back_to_itself_rather_than_raising() -> None:
    """A defensive fallback, not a silent correctness bug: grouping by the
    wrong key costs the per-underlying cap a clean read, never an order."""
    agent, store = _agent(_FakeMcp(positions=[{"symbol": "WEIRD-SHAPE"}]))

    await agent.step()

    assert {row["symbol"] for row in await store.get_cached_positions()} == {"WEIRD-SHAPE"}


async def test_a_broker_failure_propagates_to_the_supervisor() -> None:
    mcp = _FakeMcp(fail_on="get_all_positions")
    agent, store = _agent(mcp)

    with pytest.raises(RuntimeError, match="get_all_positions is down"):
        await agent.step()

    run = (await store.recent_agent_runs())[0]
    assert run["ok"] is False
    assert "get_all_positions is down" in (run["error"] or "")


async def test_a_successful_step_is_recorded_with_its_detail() -> None:
    agent, store = _agent(_FakeMcp(positions=[{"symbol": "AAPL250321C00150000"}]))

    await agent.step()

    run = (await store.recent_agent_runs())[0]
    assert run["agent"] == "position_manager"
    assert run["ok"] is True
    assert run["detail"]["open_underlyings"] == 1
    assert run["detail"]["open_legs"] == 1
