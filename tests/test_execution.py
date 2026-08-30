"""Tests for ExecutionAgent's helpers.

The position limits are written in structures — "at most one open trade per
underlying", "at most five open trades" — but Alpaca reports one position per
leg. Everything here guards the translation between the two, because getting
it wrong lets a single four-leg condor consume the whole portfolio budget.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from options_m.agents.execution import ExecutionAgent, _group_into_structures
from options_m.config import Settings
from options_m.db import Database
from options_m.store import Store


def _position(symbol: str) -> dict[str, Any]:
    return {"symbol": symbol, "asset_class": "us_option"}


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


# ---------------------------------------------------------------------------
# Reconcile: broker rejection feedback
# ---------------------------------------------------------------------------

def _make_agent() -> tuple[ExecutionAgent, MagicMock, Store]:
    settings = Settings(database_url=None)  # type: ignore[call-arg]
    store = Store(Database(settings))
    mcp = MagicMock()
    risk = MagicMock()
    agent = ExecutionAgent(settings=settings, mcp=mcp, store=store, risk_engine=risk)
    return agent, mcp, store


async def test_reconcile_broker_rejected_updates_proposal_and_records_risk_event() -> None:
    agent, mcp, store = _make_agent()

    # Seed an in-flight order in the memory store.
    proposal_id = await store.save_proposal(
        underlying="SPY", intent={"action": "open"}, evidence={}, status="submitted"
    )
    await store.record_order(
        proposal_id=proposal_id,
        client_order_id="omc-test-1",
        status="submitted",
        request={},
    )

    broker_response = {"status": "rejected", "reason": "insufficient_buying_power"}
    mcp.get_order_by_client_id = AsyncMock(return_value=broker_response)

    detail: dict[str, Any] = {"reconciled": 0, "broker_rejected": 0}
    await agent._reconcile(detail)

    assert detail["reconciled"] == 1
    assert detail["broker_rejected"] == 1

    proposals = await store.recent_proposals(limit=5)
    assert proposals[0]["status"] == "broker_rejected"
    full = await store.get_proposal(proposal_id)
    assert full is not None
    assert full["error"] == "insufficient_buying_power"

    risk_events = await store.recent_risk_events(limit=5)
    assert any(
        e["rule"] == "broker_rejected" and e["proposal_id"] == proposal_id
        for e in risk_events
    )


async def test_reconcile_filled_order_does_not_trigger_broker_rejected() -> None:
    agent, mcp, store = _make_agent()

    proposal_id = await store.save_proposal(
        underlying="SPY", intent={"action": "open"}, evidence={}, status="submitted"
    )
    await store.record_order(
        proposal_id=proposal_id,
        client_order_id="omc-test-2",
        status="submitted",
        request={},
    )

    mcp.get_order_by_client_id = AsyncMock(return_value={"status": "filled", "filled_qty": "1"})

    detail: dict[str, Any] = {"reconciled": 0, "broker_rejected": 0}
    await agent._reconcile(detail)

    assert detail["reconciled"] == 1
    assert detail["broker_rejected"] == 0

    proposals = await store.recent_proposals(limit=5)
    assert proposals[0]["status"] == "submitted"
