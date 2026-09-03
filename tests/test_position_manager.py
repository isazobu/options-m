"""Tests for PositionManagerAgent.

Scoped to what this agent now does: own the ``positions`` cache and enrich each
row with proposal metadata + pnl_pct. Exit decisions belong to StrategistAgent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


async def test_position_manager_creates_close_proposals_on_its_own_tick() -> None:
    mcp = _FakeMcp(
        positions=[
            {
                "symbol": "AAPL991231C00150000",
                "qty": "1",
                "market_value": "1600",
                "unrealized_pl": "600",
            }
        ]
    )
    agent, store = _agent(mcp)

    await agent.step()

    proposals = await store.recent_proposals(limit=5, status="pending")
    assert len(proposals) == 1
    proposal = await store.get_proposal(proposals[0]["id"])
    assert proposal is not None
    assert proposal["intent"]["action"] == "close"
    assert proposal["intent"]["thesis"].startswith("profit_target")


async def test_final_campaign_session_flattens_before_the_close(monkeypatch: Any) -> None:
    now = datetime(2026, 9, 3, 19, 45, tzinfo=UTC)
    monkeypatch.setattr("options_m.agents.position_manager._now_utc", lambda: now)
    mcp = _FakeMcp(positions=[{"symbol": "AAPL991231C00150000", "qty": "1"}])
    agent, store = _agent(
        mcp,
        campaign_start_date=now.date() - timedelta(days=1),
        campaign_days=2,
        campaign_flatten_minutes_before_close=20,
    )
    await store.upsert_market_calendar(
        [
            {
                "date": now.date() - timedelta(days=1),
                "open": datetime(2026, 9, 2, 13, 30, tzinfo=UTC),
                "close": datetime(2026, 9, 2, 20, 0, tzinfo=UTC),
                "session_type": "full",
            },
            {
                "date": now.date(),
                "open": datetime(2026, 9, 3, 13, 30, tzinfo=UTC),
                "close": datetime(2026, 9, 3, 20, 0, tzinfo=UTC),
                "session_type": "full",
            },
        ]
    )

    await agent.step()

    proposals = await store.recent_proposals(limit=5, status="pending")
    assert len(proposals) == 1
    proposal = await store.get_proposal(proposals[0]["id"])
    assert proposal is not None
    assert proposal["intent"]["thesis"] == "campaign_flatten"


async def test_a_session_after_the_campaign_ends_does_not_flatten_forever(
    monkeypatch: Any,
) -> None:
    """Regression: elapsed sessions only grows, so a bare ``elapsed >=
    campaign_days`` check would keep matching on every session after the
    campaign's one true final day, flattening the book at every close
    indefinitely instead of just once."""
    now = datetime(2026, 9, 4, 19, 45, tzinfo=UTC)
    monkeypatch.setattr("options_m.agents.position_manager._now_utc", lambda: now)
    mcp = _FakeMcp(positions=[{"symbol": "AAPL991231C00150000", "qty": "1"}])
    agent, store = _agent(
        mcp,
        campaign_start_date=now.date() - timedelta(days=2),
        campaign_days=2,
        campaign_flatten_minutes_before_close=20,
    )
    await store.upsert_market_calendar(
        [
            {
                "date": now.date() - timedelta(days=2),
                "open": datetime(2026, 9, 2, 13, 30, tzinfo=UTC),
                "close": datetime(2026, 9, 2, 20, 0, tzinfo=UTC),
                "session_type": "full",
            },
            {
                "date": now.date() - timedelta(days=1),
                "open": datetime(2026, 9, 3, 13, 30, tzinfo=UTC),
                "close": datetime(2026, 9, 3, 20, 0, tzinfo=UTC),
                "session_type": "full",
            },
            {
                "date": now.date(),
                "open": datetime(2026, 9, 4, 13, 30, tzinfo=UTC),
                "close": datetime(2026, 9, 4, 20, 0, tzinfo=UTC),
                "session_type": "full",
            },
        ]
    )

    await agent.step()

    proposals = await store.recent_proposals(limit=5, status="pending")
    assert proposals == []


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


# ---------------------------------------------------------------------------
# Net value and expiry — the fields the exit rule reads
# ---------------------------------------------------------------------------


def _occ(days_out: int, right: str = "C", strike: str = "00150000") -> str:
    """An OCC symbol expiring ``days_out`` days from today."""
    expiry = (datetime.now(UTC).date() + timedelta(days=days_out)).strftime("%y%m%d")
    return f"AAPL{expiry}{right}{strike}"


def _credit_spread_legs() -> list[dict[str, Any]]:
    """A put credit spread as Alpaca reports it: the short leg's market_value
    is negative, the long leg's positive."""
    return [
        {
            "symbol": _occ(30, "P", "00150000"),
            "side": "short",
            "qty": "-1",
            "market_value": "-300.0",
            "unrealized_pl": "50.0",
        },
        {
            "symbol": _occ(30, "P", "00145000"),
            "side": "long",
            "qty": "1",
            "market_value": "100.0",
            "unrealized_pl": "0.0",
        },
    ]


async def test_net_value_nets_the_legs_while_market_value_stays_gross() -> None:
    """Gross is what the summary shows; net is what the structure is worth."""
    agent, store = _agent(_FakeMcp(positions=_credit_spread_legs()))
    await agent.step()

    payload = (await store.get_cached_positions())[0]["payload"]
    assert payload["market_value"] == pytest.approx(400.0)
    assert payload["net_value"] == pytest.approx(-200.0)


async def test_pnl_pct_on_a_credit_spread_is_the_share_of_the_credit() -> None:
    """net_value -200 with +50 unrealized means the spread was opened for a
    250 credit and a fifth of it is realised — not 50/400 off the gross."""
    agent, store = _agent(_FakeMcp(positions=_credit_spread_legs()))
    await agent.step()

    payload = (await store.get_cached_positions())[0]["payload"]
    assert payload["pnl_pct"] == pytest.approx(50.0 / 250.0)


async def test_min_dte_is_the_nearest_expiry_across_the_legs() -> None:
    positions = [
        {"symbol": _occ(45), "side": "long", "qty": "1"},
        {"symbol": _occ(10), "side": "short", "qty": "-1"},
    ]
    agent, store = _agent(_FakeMcp(positions=positions))
    await agent.step()

    payload = (await store.get_cached_positions())[0]["payload"]
    assert payload["min_dte"] == 10


async def test_min_dte_is_none_for_a_position_with_no_option_leg() -> None:
    agent, store = _agent(_FakeMcp(positions=[{"symbol": "AAPL", "qty": "100"}]))
    await agent.step()
    payload = (await store.get_cached_positions())[0]["payload"]
    assert payload["min_dte"] is None


# ---------------------------------------------------------------------------
# Strategy resolution
# ---------------------------------------------------------------------------


async def test_the_strategy_is_resolved_from_the_originating_proposal() -> None:
    """The order carries no strategy name — the proposal behind it does, and
    that is what tells StrategistAgent which exit thresholds to apply."""
    symbol = _occ(30)
    settings = Settings(database_url=None)
    store = Store(Database(settings))
    proposal_id = await store.save_proposal(
        underlying="AAPL",
        intent={"action": "open", "strategy": "put_credit_spread"},
        evidence={},
    )
    await store.record_order(
        proposal_id=proposal_id,
        client_order_id=f"om-{proposal_id}",
        status="filled",
        request={"legs": [{"symbol": symbol}]},
    )
    mcp = _FakeMcp(positions=[{"symbol": symbol, "qty": "1"}])

    await PositionManagerAgent(settings, mcp, store).step()  # type: ignore[arg-type]

    payload = (await store.get_cached_positions())[0]["payload"]
    assert payload["proposal_id"] == proposal_id
    assert payload["strategy"] == "put_credit_spread"


async def test_a_position_with_no_proposal_gets_an_empty_strategy() -> None:
    """Set, not left absent: the lookup is attempted once, and an unresolved
    position falls back to the symmetric thresholds rather than retrying on
    every tick."""
    agent, store = _agent(_FakeMcp(positions=[{"symbol": _occ(30), "qty": "1"}]))
    await agent.step()
    payload = (await store.get_cached_positions())[0]["payload"]
    assert payload["strategy"] == ""


def test_compute_pnl_pct_prefers_the_net_over_the_gross() -> None:
    payload = {"unrealized_pl": 50.0, "market_value": 400.0, "net_value": -200.0}
    assert _compute_pnl_pct(payload) == pytest.approx(50.0 / 250.0)


def test_compute_pnl_pct_falls_back_to_the_gross_for_an_older_payload() -> None:
    """A row written before net_value existed still marks to market."""
    assert _compute_pnl_pct(
        {"unrealized_pl": 500.0, "market_value": 1500.0}
    ) == pytest.approx(0.50)
