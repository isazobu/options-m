"""Tests for MarketPulseAgent.

The agent is the only thing standing between live broker data and the audit
trail, so the assertions here are mostly about restraint: it does not trade a
closed market, it does not invent numbers, and it does not swallow failures
that the supervisor needs to see.

Design change (2026-08-29): market state comes from a local market_calendar
cache this agent populates from get_calendar, not from a live get_clock call
on every tick -- so the fake MCP below deliberately has no get_clock or
get_news method at all. If the agent ever called either, these tests would
fail with an AttributeError, which is a stronger guarantee than a "not
called" assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from options_m.config import Settings
from options_m.db import Database
from options_m.store import Store
from options_m.trading.market_pulse import _EXCHANGE_TZ, MarketPulseAgent


def _calendar_rows(*, is_open: bool) -> list[dict[str, Any]]:
    """One raw get_calendar-shaped row, or none at all for a "closed" day.

    A missing row is what a real weekend/holiday looks like, and it is the
    simplest way to make "closed" deterministic regardless of wall-clock time
    -- the alternative (a row whose window excludes "now") would be flaky
    depending on what time the test suite happens to run.
    """
    if not is_open:
        return []
    now_local = datetime.now(UTC).astimezone(_EXCHANGE_TZ)
    open_time = (now_local - timedelta(hours=1)).strftime("%H:%M")
    close_time = (now_local + timedelta(hours=1)).strftime("%H:%M")
    return [{"date": now_local.date().isoformat(), "open": open_time, "close": close_time}]


class _FakeMcp:
    """Stands in for AlpacaMcp with scripted responses."""

    def __init__(
        self,
        *,
        is_open: bool = True,
        account: dict[str, Any] | None = None,
        account_config: dict[str, Any] | None = None,
        movers: Any = None,
        actives: Any = None,
        fail_on: str | None = None,
    ) -> None:
        self._calendar_rows = _calendar_rows(is_open=is_open)
        self._account = account or {
            "equity": "100000.00",
            "cash": "100000.00",
            "buying_power": "200000.00",
        }
        self._account_config = (
            account_config if account_config is not None else {"options_trading_level": 3}
        )
        self._movers = movers if movers is not None else {"gainers": [], "losers": []}
        self._actives = actives if actives is not None else {"most_actives": []}
        self._fail_on = fail_on
        self.called: list[str] = []

    def _record(self, name: str) -> None:
        self.called.append(name)
        if self._fail_on == name:
            msg = f"{name} is down"
            raise RuntimeError(msg)

    async def get_calendar(self, start: str, end: str) -> list[dict[str, Any]]:
        self._record("get_calendar")
        # Echo a placeholder row at the far edge of the requested window, the
        # way a real ~1yr get_calendar response would -- otherwise the fake
        # only ever returns "today", calendar_max_date() never moves past
        # today, and _ensure_calendar_fresh would (correctly, given that
        # input) refetch on every single tick instead of respecting the
        # margin.
        horizon_row = {"date": end, "open": "09:30", "close": "16:00"}
        return [*self._calendar_rows, horizon_row]

    async def get_account_info(self) -> dict[str, Any]:
        self._record("get_account_info")
        return self._account

    async def get_account_config(self) -> dict[str, Any]:
        self._record("get_account_config")
        return self._account_config

    async def get_market_movers(self, top: int = 10) -> Any:
        self._record("get_market_movers")
        return self._movers

    async def get_most_active_stocks(self, top: int = 10) -> Any:
        self._record("get_most_active_stocks")
        return self._actives


def _agent(mcp: Any, **overrides: Any) -> tuple[MarketPulseAgent, Store]:
    settings = Settings(database_url=None, universe="SPY,QQQ,AAPL", **overrides)
    store = Store(Database(settings))
    return MarketPulseAgent(settings, mcp, store), store


async def test_uses_its_own_cadence_not_the_global_default() -> None:
    agent, _store = _agent(_FakeMcp(), market_pulse_interval_seconds=45.0)

    assert agent.interval_seconds == 45.0


async def test_a_closed_market_records_equity_but_no_candidates() -> None:
    """No point burning broker calls or Neon compute against a closed market."""
    mcp = _FakeMcp(is_open=False)
    agent, store = _agent(mcp)

    await agent.step()

    assert "get_market_movers" not in mcp.called
    assert await store.recent_candidates() == []
    # The equity curve stays continuous even overnight.
    assert len(await store.recent_equity()) == 1


async def test_an_open_market_scores_and_saves_candidates() -> None:
    mcp = _FakeMcp(
        movers={
            "gainers": [{"symbol": "SPY", "percent_change": 2.5}],
            "losers": [{"symbol": "AAPL", "percent_change": -4.0}],
        },
        actives={"most_actives": [{"symbol": "SPY", "volume": 1_000_000}]},
    )
    agent, store = _agent(mcp)

    await agent.step()

    saved = {row["symbol"]: row for row in await store.recent_candidates()}
    assert set(saved) == {"SPY", "AAPL"}
    # A 4% drop outranks a 2.5% rise plus a most-active listing: for an
    # options agent, the size of the move matters, not its direction, and
    # there is no news signal anymore to tip the balance the other way.
    assert saved["AAPL"]["score"] > saved["SPY"]["score"]


async def test_symbols_outside_the_universe_are_ignored() -> None:
    mcp = _FakeMcp(movers={"gainers": [{"symbol": "GME", "percent_change": 40.0}]})
    agent, store = _agent(mcp)

    await agent.step()

    assert [row["symbol"] for row in await store.recent_candidates()] == []


async def test_unreadable_account_fields_are_recorded_as_unknown() -> None:
    """A NaN or missing balance must never be persisted as 0.0."""
    mcp = _FakeMcp(is_open=False, account={"equity": "n/a", "cash": None})
    agent, store = _agent(mcp)

    await agent.step()

    point = (await store.recent_equity())[0]
    assert point["equity"] is None
    assert point["cash"] is None


async def test_a_broker_failure_propagates_to_the_supervisor() -> None:
    """Swallowing this would hide a dead broker behind a healthy-looking loop."""
    mcp = _FakeMcp(fail_on="get_account_info")
    agent, store = _agent(mcp)

    with pytest.raises(RuntimeError, match="get_account_info is down"):
        await agent.step()

    # The failure is still recorded, which is what the health panel shows.
    run = (await store.recent_agent_runs())[0]
    assert run["ok"] is False
    assert "get_account_info is down" in (run["error"] or "")


async def test_a_successful_step_is_recorded_with_its_detail() -> None:
    agent, store = _agent(_FakeMcp())

    await agent.step()

    run = (await store.recent_agent_runs())[0]
    assert run["agent"] == "market_pulse"
    assert run["ok"] is True
    assert run["detail"]["market_open"] is True


async def test_an_unfamiliar_movers_shape_costs_a_signal_not_correctness() -> None:
    mcp = _FakeMcp(movers={"unexpected": "shape"}, actives=None)
    agent, store = _agent(mcp)

    await agent.step()

    assert await store.recent_candidates() == []
    assert (await store.recent_agent_runs())[0]["ok"] is True


async def test_calendar_is_fetched_once_then_reused_within_the_margin() -> None:
    """The whole point of the cache: a fresh window means no second live call."""
    mcp = _FakeMcp()
    agent, _store = _agent(mcp)

    await agent.step()
    await agent.step()

    assert mcp.called.count("get_calendar") == 1


async def test_positions_count_comes_from_the_local_cache_not_a_live_call() -> None:
    """PositionManagerAgent owns the positions cache; this agent only reads it.

    The fake MCP has no get_all_positions method at all, so calling it would
    raise -- this is the design change from Phase 1, where MarketPulseAgent
    called get_all_positions itself.
    """
    mcp = _FakeMcp()
    agent, store = _agent(mcp)
    await store.upsert_position("SPY", {"symbol": "SPY", "qty": "1"})

    await agent.step()

    point = (await store.recent_equity())[0]
    assert point["positions_count"] == 1


async def test_account_cache_is_upserted_from_the_same_tick_no_extra_calls() -> None:
    mcp = _FakeMcp(
        account={"equity": "123456.78", "cash": "50000.00", "buying_power": "150000.00"},
        account_config={"options_trading_level": 2},
    )
    agent, store = _agent(mcp)

    await agent.step()

    cached = await store.get_cached_account()
    assert cached is not None
    assert float(cached["equity"]) == 123456.78
    assert cached["options_trading_level"] == 2
    # One get_account_info / get_account_config call each -- no duplicate traffic.
    assert mcp.called.count("get_account_info") == 1
    assert mcp.called.count("get_account_config") == 1
