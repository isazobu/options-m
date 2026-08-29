"""Tests for MarketPulseAgent.

The agent is the only thing standing between live broker data and the audit
trail, so the assertions here are mostly about restraint: it does not trade a
closed market, it does not invent numbers, and it does not swallow failures that
the supervisor needs to see.
"""

from __future__ import annotations

from typing import Any

import pytest

from options_m.config import Settings
from options_m.db import Database
from options_m.store import Store
from options_m.trading.market_pulse import MarketPulseAgent


class _FakeMcp:
    """Stands in for AlpacaMcp with scripted responses."""

    def __init__(
        self,
        *,
        is_open: bool = True,
        account: dict[str, Any] | None = None,
        positions: list[dict[str, Any]] | None = None,
        movers: Any = None,
        actives: Any = None,
        news: Any = None,
        fail_on: str | None = None,
    ) -> None:
        self._is_open = is_open
        self._account = account or {
            "equity": "100000.00",
            "cash": "100000.00",
            "buying_power": "200000.00",
        }
        self._positions = positions or []
        self._movers = movers if movers is not None else {"gainers": [], "losers": []}
        self._actives = actives if actives is not None else {"most_actives": []}
        self._news = news if news is not None else []
        self._fail_on = fail_on
        self.called: list[str] = []

    def _record(self, name: str) -> None:
        self.called.append(name)
        if self._fail_on == name:
            msg = f"{name} is down"
            raise RuntimeError(msg)

    async def get_clock(self) -> dict[str, Any]:
        self._record("get_clock")
        return {
            "is_open": self._is_open,
            "next_open": "2026-09-01T13:30:00Z",
            "next_close": "2026-08-31T20:00:00Z",
        }

    async def get_account_info(self) -> dict[str, Any]:
        self._record("get_account_info")
        return self._account

    async def get_all_positions(self) -> list[dict[str, Any]]:
        self._record("get_all_positions")
        return self._positions

    async def get_market_movers(self, top: int = 10) -> Any:
        self._record("get_market_movers")
        return self._movers

    async def get_most_active_stocks(self, top: int = 10) -> Any:
        self._record("get_most_active_stocks")
        return self._actives

    async def get_news(self, symbols: Any, limit: int = 20) -> Any:
        self._record("get_news")
        return self._news


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
    assert "get_news" not in mcp.called
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
        news=[{"symbols": ["AAPL"]}, {"symbols": ["AAPL"]}],
    )
    agent, store = _agent(mcp)

    await agent.step()

    saved = {row["symbol"]: row for row in await store.recent_candidates()}
    assert set(saved) == {"SPY", "AAPL"}
    # A 4% drop outranks a 2.5% rise plus an active listing: for an options
    # agent, the size of the move matters, not its direction.
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
    mcp = _FakeMcp(movers={"unexpected": "shape"}, actives=None, news=None)
    agent, store = _agent(mcp)

    await agent.step()

    assert await store.recent_candidates() == []
    assert (await store.recent_agent_runs())[0]["ok"] is True
