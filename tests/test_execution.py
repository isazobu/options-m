"""Tests for ExecutionAgent's helpers.

The position limits are written in structures — "at most one open trade per
underlying", "at most five open trades" — but Alpaca reports one position per
leg. Everything here guards the translation between the two, because getting
it wrong lets a single four-leg condor consume the whole portfolio budget.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from options_m.agents.execution import (
    ExecutionAgent,
    _group_into_structures,
    build_portfolio_snapshot,
)
from options_m.config import Settings
from options_m.db import Database
from options_m.risk import RiskEngine, RiskLimits
from options_m.store import Store


def _position(symbol: str) -> dict[str, Any]:
    return {"symbol": symbol, "asset_class": "us_option"}


class _FakeMcp:
    """Only the two reads build_portfolio_snapshot makes."""

    def __init__(self, positions: list[dict[str, Any]] | None = None) -> None:
        self._positions = positions or []

    async def get_clock(self) -> dict[str, Any]:
        return {"next_close": None}

    async def get_all_positions(self) -> list[dict[str, Any]]:
        return list(self._positions)


def _store() -> Store:
    return Store(Database(Settings(database_url=None)))


async def _snapshot(
    store: Store, mcp: Any, underlying: str = "SPY", **kwargs: Any
) -> Any:
    return await build_portfolio_snapshot(
        underlying,
        kwargs.pop("client_order_id", "om-new"),
        {"equity": "100000", "last_equity": "100000"},
        mcp=mcp,
        store=store,
        settings=Settings(database_url=None),
        **kwargs,
    )


async def test_snapshot_counts_a_resting_order_as_a_position_in_the_underlying() -> None:
    """H1: a submitted-but-unfilled om-<id> order occupies the per-underlying
    slot exactly as a filled position would — otherwise the same symbol can be
    re-proposed and re-submitted while the first order still rests."""
    store = _store()
    await store.record_order(
        proposal_id=1,
        client_order_id="om-1",
        status="submitted",
        request={"symbol": "SPY250620P00500000", "qty": "1"},
    )

    snapshot = await _snapshot(store, _FakeMcp())

    assert snapshot.positions_in_underlying == 1
    assert snapshot.concurrent_option_positions == 1


async def test_snapshot_counts_an_active_proposal_but_excludes_the_current_one() -> None:
    store = _store()
    other = await store.save_proposal(underlying="QQQ", intent={}, evidence={})
    await store.update_proposal_status(other, "dry_run_approved")
    mine = await store.save_proposal(underlying="SPY", intent={}, evidence={})

    snapshot = await _snapshot(store, _FakeMcp(), exclude_proposal_id=mine)

    # QQQ's approved proposal counts toward the concurrent cap; SPY's own
    # pending proposal is excluded so it does not block itself.
    assert snapshot.concurrent_option_positions == 1
    assert snapshot.positions_in_underlying == 0


async def test_snapshot_does_not_double_count_a_proposal_that_already_filled() -> None:
    store = _store()
    filled = await store.save_proposal(underlying="SPY", intent={}, evidence={})
    await store.update_proposal_status(filled, "submitted")

    snapshot = await _snapshot(
        store, _FakeMcp(positions=[_position("SPY250620P00500000")])
    )

    assert snapshot.positions_in_underlying == 1
    assert snapshot.concurrent_option_positions == 1


def test_the_four_legs_of_one_condor_count_as_one_structure() -> None:
    legs = [
        _position("SPY250321P00095000"),
        _position("SPY250321P00090000"),
        _position("SPY250321C00105000"),
        _position("SPY250321C00110000"),
    ]

    assert _group_into_structures(legs) == {("SPY", date(2025, 3, 21))}


def test_two_expiries_on_one_underlying_are_two_structures() -> None:
    """Legs of one structure share an expiry; different expiries are separate trades."""
    legs = [
        _position("SPY250321P00095000"),
        _position("SPY250321P00090000"),
        _position("SPY250418P00095000"),
        _position("SPY250418P00090000"),
    ]

    assert _group_into_structures(legs) == {
        ("SPY", date(2025, 3, 21)),
        ("SPY", date(2025, 4, 18)),
    }


def test_different_underlyings_never_merge() -> None:
    legs = [_position("SPY250321C00105000"), _position("QQQ250321C00105000")]

    assert len(_group_into_structures(legs)) == 2


def test_an_unparseable_symbol_counts_as_its_own_structure() -> None:
    """The limits cap exposure, so an unrecognised position must never shrink
    the count — the safe direction is to over-count, never to under-count."""
    legs = [
        _position("SPY250321C00105000"),
        _position("not-an-occ-symbol"),
        _position(""),
    ]

    assert len(_group_into_structures(legs)) == 3


def test_no_positions_is_no_structures() -> None:
    assert _group_into_structures([]) == set()


# --- reconcile: settling a proposal once its order reaches a terminal state ---


class _ReconcileMcp:
    """Serves a scripted broker order per client_order_id, advancing through a
    list of states on successive polls."""

    def __init__(self, scripts: dict[str, list[dict[str, Any]]]) -> None:
        self._scripts = {cid: list(states) for cid, states in scripts.items()}

    async def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        states = self._scripts.get(client_order_id)
        if not states:
            return None
        return states.pop(0) if len(states) > 1 else states[0]


def _agent(store: Store, mcp: Any) -> ExecutionAgent:
    settings = Settings(database_url=None)
    return ExecutionAgent(settings, mcp, store, RiskEngine(RiskLimits.from_settings(settings)))


async def _submitted_order(store: Store, *, client_order_id: str = "om-1") -> int:
    proposal_id = await store.save_proposal(underlying="SPY", intent={}, evidence={})
    await store.update_proposal_status(proposal_id, "submitted")
    await store.record_order(
        proposal_id=proposal_id,
        client_order_id=client_order_id,
        status="submitted",
        request={"symbol": "SPY250620P00500000"},
    )
    return proposal_id


async def test_reconcile_marks_the_proposal_filled_when_the_order_fills() -> None:
    store = _store()
    proposal_id = await _submitted_order(store)
    fill = {"status": "filled", "filled_qty": "1", "filled_avg_price": "3.2"}
    mcp = _ReconcileMcp({"om-1": [fill]})

    detail: dict[str, Any] = {"reconciled": 0, "filled": 0, "broker_unfilled": 0}
    await _agent(store, mcp)._reconcile(detail)

    assert (await store.get_proposal(proposal_id))["status"] == "filled"
    assert detail["filled"] == 1


async def test_reconcile_releases_the_proposal_when_the_broker_rejects_an_accepted_order() -> None:
    store = _store()
    proposal_id = await _submitted_order(store)
    reject = {
        "status": "rejected",
        "filled_qty": "0",
        "reject_reason": "insufficient buying power",
    }
    mcp = _ReconcileMcp({"om-1": [reject]})

    detail: dict[str, Any] = {"reconciled": 0, "filled": 0, "broker_unfilled": 0}
    await _agent(store, mcp)._reconcile(detail)

    proposal = await store.get_proposal(proposal_id)
    assert proposal["status"] == "rejected"
    assert "insufficient buying power" in proposal["error"]
    assert detail["broker_unfilled"] == 1
    events = await store.recent_risk_events()
    assert events[0]["rule"] == "broker_order_not_filled"
    # The underlying is no longer blocked from a fresh proposal.
    assert await store.active_proposal_underlyings() == set()


async def test_reconcile_keeps_polling_an_order_that_was_only_accepted_on_the_first_tick() -> None:
    """Regression: with orders_in_flight matching only 'submitted', an order
    reported 'accepted' on tick one dropped out of reconcile and never had its
    later fill recorded."""
    store = _store()
    proposal_id = await _submitted_order(store)
    mcp = _ReconcileMcp(
        {
            "om-1": [
                {"status": "accepted", "filled_qty": "0"},
                {"status": "filled", "filled_qty": "1", "filled_avg_price": "3.2"},
            ]
        }
    )
    agent = _agent(store, mcp)

    first: dict[str, Any] = {"reconciled": 0, "filled": 0, "broker_unfilled": 0}
    await agent._reconcile(first)
    assert (await store.get_proposal(proposal_id))["status"] == "submitted"
    assert first["filled"] == 0

    second: dict[str, Any] = {"reconciled": 0, "filled": 0, "broker_unfilled": 0}
    await agent._reconcile(second)
    assert (await store.get_proposal(proposal_id))["status"] == "filled"
    assert second["filled"] == 1
