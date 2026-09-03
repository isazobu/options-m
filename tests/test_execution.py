"""Tests for ExecutionAgent's helpers.

The position limits are written in structures — "at most one open trade per
underlying", "at most five open trades" — but Alpaca reports one position per
leg. Everything here guards the translation between the two, because getting
it wrong lets a single four-leg condor consume the whole portfolio budget.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastmcp.exceptions import ToolError

from options_m.agents.execution import (
    ExecutionAgent,
    _build_closing_legs,
    _close_limit_price,
    _group_into_structures,
    build_portfolio_snapshot,
)
from options_m.config import Settings
from options_m.db import Database
from options_m.models import Rejection, StrategyIntent
from options_m.store import Store


def _position(symbol: str) -> dict[str, Any]:
    return {"symbol": symbol, "asset_class": "us_option"}


def _store() -> Store:
    return Store(Database(Settings(database_url=None)))


class _SnapshotMcp:
    """The two reads build_portfolio_snapshot makes."""

    def __init__(self, positions: list[dict[str, Any]] | None = None) -> None:
        self._positions = positions or []

    async def get_clock(self) -> dict[str, Any]:
        return {"next_close": None}

    async def get_all_positions(self) -> list[dict[str, Any]]:
        return list(self._positions)

    async def get_option_snapshot(self, symbols: Any) -> dict[str, dict[str, Any]]:
        return {}


async def _snapshot(store: Store, mcp: Any, underlying: str = "SPY", **kwargs: Any) -> Any:
    return await build_portfolio_snapshot(
        underlying,
        kwargs.pop("client_order_id", "om-new"),
        {"equity": "100000", "last_equity": "100000"},
        mcp=mcp,
        store=store,
        settings=Settings(database_url=None),
        **kwargs,
    )


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

class _Collector:
    """A notifier that records what the agent would have sent to Telegram."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, text: str) -> None:
        self.messages.append(text)


def _make_agent(
    notifier: _Collector | None = None, **overrides: Any
) -> tuple[ExecutionAgent, MagicMock, Store]:
    settings = Settings(database_url=None, **overrides)
    store = Store(Database(settings))
    mcp = MagicMock()
    risk = MagicMock()
    agent = ExecutionAgent(
        settings=settings, mcp=mcp, store=store, risk_engine=risk, notifier=notifier
    )
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


async def test_reconcile_still_polls_an_order_that_was_only_accepted_on_the_first_tick() -> None:
    """Regression: with orders_in_flight matching only 'submitted', an order
    reported 'accepted' on tick one dropped out of reconcile and never had its
    later rejection recorded."""
    agent, mcp, store = _make_agent()
    proposal_id = await store.save_proposal(
        underlying="SPY", intent={"action": "open"}, evidence={}, status="submitted"
    )
    await store.record_order(
        proposal_id=proposal_id, client_order_id="om-acc", status="submitted", request={}
    )
    mcp.get_order_by_client_id = AsyncMock(
        side_effect=[
            {"status": "accepted", "filled_qty": "0"},
            {"status": "rejected", "reason": "insufficient_buying_power", "filled_qty": "0"},
        ]
    )

    await agent._reconcile({"reconciled": 0, "broker_rejected": 0})
    still_submitted = await store.get_proposal(proposal_id)
    assert still_submitted is not None and still_submitted["status"] == "submitted"

    detail: dict[str, Any] = {"reconciled": 0, "broker_rejected": 0}
    await agent._reconcile(detail)
    rejected = await store.get_proposal(proposal_id)
    assert rejected is not None and rejected["status"] == "broker_rejected"
    assert detail["broker_rejected"] == 1


# ---------------------------------------------------------------------------
# H1: proposals gated on working orders / in-flight proposals, not just fills
# ---------------------------------------------------------------------------


async def test_snapshot_counts_a_resting_order_as_a_position_in_the_underlying() -> None:
    store = _store()
    await store.record_order(
        proposal_id=1,
        client_order_id="om-1",
        status="submitted",
        request={"symbol": "SPY250620P00500000", "qty": "1"},
    )

    snapshot = await _snapshot(store, _SnapshotMcp())

    assert snapshot.positions_in_underlying == 1
    assert snapshot.concurrent_option_positions == 1


async def test_snapshot_counts_an_active_proposal_but_excludes_the_current_one() -> None:
    store = _store()
    other = await store.save_proposal(underlying="QQQ", intent={}, evidence={})
    await store.update_proposal_status(other, "dry_run_approved")
    mine = await store.save_proposal(underlying="SPY", intent={}, evidence={})

    snapshot = await _snapshot(store, _SnapshotMcp(), exclude_proposal_id=mine)

    # QQQ's approved proposal counts toward the concurrent cap; SPY's own
    # pending proposal is excluded so it does not block itself.
    assert snapshot.concurrent_option_positions == 1
    assert snapshot.positions_in_underlying == 0


async def test_snapshot_does_not_double_count_a_proposal_that_already_filled() -> None:
    store = _store()
    filled = await store.save_proposal(underlying="SPY", intent={}, evidence={})
    await store.update_proposal_status(filled, "submitted")

    snapshot = await _snapshot(
        store, _SnapshotMcp(positions=[_position("SPY250620P00500000")])
    )

    assert snapshot.positions_in_underlying == 1
    assert snapshot.concurrent_option_positions == 1


# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------


async def test_a_broker_rejection_is_announced() -> None:
    collector = _Collector()
    agent, _, store = _make_agent(collector)
    proposal_id = await store.save_proposal(
        underlying="SPY", intent={"action": "open"}, evidence={}, status="submitted"
    )
    await agent._mark_broker_rejected(
        proposal_id, "om-1", {"symbol": "SPY", "reason": "no buying power"}, "rejected", {}
    )
    assert len(collector.messages) == 1
    assert "no buying power" in collector.messages[0]
    assert "SPY" in collector.messages[0]


async def test_a_risk_rejection_is_announced_with_its_underlying() -> None:
    collector = _Collector()
    agent, _, store = _make_agent(collector)
    proposal_id = await store.save_proposal(
        underlying="NVDA", intent={"action": "open"}, evidence={}, status="pending"
    )
    await agent._reject(
        proposal_id, Rejection(proposal_id=proposal_id, reason="no_spot_price"), "NVDA"
    )
    assert "no\\_spot\\_price" in collector.messages[0]
    assert "NVDA" in collector.messages[0]


async def test_notifications_can_be_switched_off() -> None:
    collector = _Collector()
    agent, _, store = _make_agent(collector, telegram_notify_orders=False)
    proposal_id = await store.save_proposal(
        underlying="SPY", intent={"action": "open"}, evidence={}, status="pending"
    )
    await agent._reject(proposal_id, Rejection(proposal_id=proposal_id, reason="x"), "SPY")
    assert collector.messages == []


async def test_the_agent_works_without_a_notifier() -> None:
    """The default is a null notifier, so no call site needs to guard."""
    agent, _, store = _make_agent()
    proposal_id = await store.save_proposal(
        underlying="SPY", intent={"action": "open"}, evidence={}, status="pending"
    )
    await agent._reject(proposal_id, Rejection(proposal_id=proposal_id, reason="x"), "SPY")
    row = await store.get_proposal(proposal_id)
    assert row is not None and row["status"] == "rejected"


# ---------------------------------------------------------------------------
# Closing a position — the limit price comes off the net, not the gross
# ---------------------------------------------------------------------------


class _CloseMcp:
    """Captures the order the close path submits."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def place_option_order(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return {"id": "order-1", "status": "new"}

    async def get_order_by_client_id(self, _client_order_id: str) -> dict[str, Any]:
        return {"id": "order-1", "status": "new", "filled_qty": "0"}


def _close_agent(store: Store, mcp: Any) -> ExecutionAgent:
    settings = Settings(database_url=None, dry_run=False)
    return ExecutionAgent(settings, mcp, store, MagicMock())


def _close_intent() -> Any:
    return StrategyIntent(
        action="close",
        strategy="put_credit_spread",
        underlying="AAPL",
        target_delta=0.5,
        dte_min=0,
        dte_max=365,
        conviction=1.0,
        thesis="profit_target",
        invalidation="",
    )


async def _close_with(payload: dict[str, Any]) -> dict[str, Any]:
    store = _store()
    await store.upsert_position("AAPL", payload)
    mcp = _CloseMcp()
    proposal_id = await store.save_proposal(underlying="AAPL", intent={}, evidence={})
    await _close_agent(store, mcp)._execute_close(
        proposal_id, _close_intent(), {"submitted": 0, "failed": 0, "rejected": 0}
    )
    assert mcp.kwargs is not None
    return mcp.kwargs


async def test_a_spread_is_bought_back_at_its_net_not_its_gross() -> None:
    """The short leg is worth -300 and the long +100: closing the structure
    costs 200, i.e. 2.00 per contract. Pricing off the gross 400 would offer
    twice what the spread can be bought back for."""
    kwargs = await _close_with(
        {
            "market_value": 400.0,
            "net_value": -200.0,
            "legs": [
                {"symbol": "AAPL991231P00150000", "side": "short", "qty": "-1"},
                {"symbol": "AAPL991231P00145000", "side": "long", "qty": "1"},
            ],
        }
    )
    assert kwargs["limit_price"] == "2.00"


async def test_a_payload_without_a_net_still_prices_off_the_gross() -> None:
    """Rows written before net_value existed keep their old behaviour."""
    kwargs = await _close_with(
        {
            "market_value": 250.0,
            "legs": [{"symbol": "AAPL991231C00150000", "side": "long", "qty": "1"}],
        }
    )
    assert kwargs["limit_price"] == "2.50"


def test_closing_legs_carry_the_intent_matching_their_side() -> None:
    legs = _build_closing_legs(
        [
            {"symbol": "AAPL991231P00150000", "side": "short", "qty": "-1"},
            {"symbol": "AAPL991231P00145000", "side": "long", "qty": "1"},
        ]
    )

    assert legs[0]["side"] == "buy"
    assert legs[0]["position_intent"] == "buy_to_close"
    assert legs[1]["side"] == "sell"
    assert legs[1]["position_intent"] == "sell_to_close"


def test_close_leg_ratios_do_not_multiply_the_position_quantity_twice() -> None:
    legs = _build_closing_legs(
        [
            {"symbol": "AAPL991231P00150000", "side": "short", "qty": "-6"},
            {"symbol": "AAPL991231P00145000", "side": "long", "qty": "6"},
        ]
    )

    assert [leg["ratio_qty"] for leg in legs] == ["1", "1"]


def test_stop_and_flatten_limits_start_more_aggressively_than_profit_taking() -> None:
    assert _close_limit_price(2.0, "profit_target", nudge=0.25, attempt=0, pays_a_debit=True) == 2.0
    assert _close_limit_price(2.0, "stop_loss", nudge=0.25, attempt=0, pays_a_debit=True) == 2.5
    assert (
        _close_limit_price(2.0, "campaign_flatten", nudge=0.25, attempt=0, pays_a_debit=True) == 2.5
    )


def test_each_reprice_ladder_rung_is_more_marketable() -> None:
    prices = [
        _close_limit_price(2.0, "profit_target", nudge=0.25, attempt=attempt, pays_a_debit=True)
        for attempt in range(4)
    ]
    assert prices == [2.0, 2.5, 3.0, 3.5]


def test_a_credit_on_close_gets_more_marketable_at_a_lower_price() -> None:
    """Regression: closing a long call/put/strangle or a debit spread nets a
    credit. Raising the limit price there (as for a pay-to-close structure)
    moves a sell-to-close order away from the market, not toward it."""
    assert (
        _close_limit_price(2.0, "profit_target", nudge=0.25, attempt=0, pays_a_debit=False) == 2.0
    )
    assert _close_limit_price(2.0, "stop_loss", nudge=0.25, attempt=0, pays_a_debit=False) == 1.5

    prices = [
        _close_limit_price(2.0, "stop_loss", nudge=0.25, attempt=attempt, pays_a_debit=False)
        for attempt in range(4)
    ]
    assert prices == [1.5, 1.0, 0.5, 0.01]


async def test_kill_switch_still_executes_a_pending_close() -> None:
    settings = Settings(database_url=None, dry_run=False, kill_switch=True)
    store = Store(Database(settings))
    await store.upsert_position(
        "AAPL",
        {
            "net_value": -200.0,
            "legs": [
                {"symbol": "AAPL991231P00150000", "side": "short", "qty": "-1"},
                {"symbol": "AAPL991231P00145000", "side": "long", "qty": "1"},
            ],
        },
    )
    await store.save_proposal(
        underlying="AAPL",
        intent=_close_intent().model_dump(mode="json"),
        evidence={},
    )
    mcp: Any = _CloseMcp()
    agent = ExecutionAgent(settings, mcp, store, MagicMock())

    detail = await agent._run()

    assert detail["submitted"] == 1
    assert mcp.kwargs is not None


async def test_kill_switch_leaves_an_open_proposal_pending() -> None:
    settings = Settings(database_url=None, dry_run=False, kill_switch=True)
    store = Store(Database(settings))
    proposal_id = await store.save_proposal(
        underlying="AAPL",
        intent={
            **_close_intent().model_dump(mode="json"),
            "action": "open",
        },
        evidence={},
    )
    agent = ExecutionAgent(settings, MagicMock(), store, MagicMock())

    detail = await agent._run()

    assert detail["submitted"] == 0
    proposal = await store.get_proposal(proposal_id)
    assert proposal is not None
    assert proposal["status"] == "pending"


def _reprice_settings() -> Settings:
    return Settings(
        database_url=None,
        dry_run=False,
        close_reprice_seconds=1,
        close_reprice_max_attempts=3,
    )


async def _stale_close_order(settings: Settings) -> tuple[Store, str]:
    """A close order old enough that reconcile owes it one rung of reprice."""
    store = Store(Database(settings))
    proposal_id = await store.save_proposal(
        underlying="AAPL",
        intent=_close_intent().model_dump(mode="json"),
        evidence={},
        status="submitted",
    )
    client_order_id = f"omc-{proposal_id}"
    await store.record_order(
        proposal_id=proposal_id,
        client_order_id=client_order_id,
        status="new",
        request={
            "action": "close",
            "underlying": "AAPL",
            "exit_reason": "profit_target",
            "mark_price": 2.0,
            "limit_price": "2.00",
        },
    )
    store._memory_orders[client_order_id]["submitted_at"] = datetime.now(UTC) - timedelta(
        seconds=2.1
    )
    return store, client_order_id


async def test_reconcile_reprices_a_stale_close_order_up_the_ladder() -> None:
    settings = _reprice_settings()
    store, _client_order_id = await _stale_close_order(settings)
    mcp = MagicMock()
    mcp.get_order_by_client_id = AsyncMock(
        return_value={
            "id": "broker-1",
            "status": "new",
            "filled_qty": "0",
            "limit_price": "2.00",
        }
    )
    mcp.replace_order_by_id = AsyncMock(
        return_value={
            "id": "broker-2",
            "status": "new",
            "filled_qty": "0",
            "limit_price": "3.00",
        }
    )
    agent = ExecutionAgent(settings, mcp, store, MagicMock())
    detail: dict[str, Any] = {"reconciled": 0, "broker_rejected": 0}

    await agent._reconcile(detail)

    mcp.replace_order_by_id.assert_awaited_once_with(
        "broker-1", limit_price="3.00"
    )
    assert detail["repriced"] == 1


async def test_reconcile_does_not_reprice_an_order_that_is_no_longer_open() -> None:
    """Alpaca answers a replace against a settled order with a 422.

    Repricing on elapsed time alone sent that replace to a filled order, and
    the 422 escaped before the fill was written — so the row stayed in flight
    and every later tick replayed the same dead replace.
    """
    settings = _reprice_settings()
    store, client_order_id = await _stale_close_order(settings)
    mcp = MagicMock()
    mcp.get_order_by_client_id = AsyncMock(
        return_value={
            "id": "broker-1",
            "status": "filled",
            "filled_qty": "1",
            "filled_avg_price": "2.05",
            "limit_price": "2.00",
        }
    )
    mcp.replace_order_by_id = AsyncMock()
    agent = ExecutionAgent(settings, mcp, store, MagicMock())

    await agent._reconcile({"reconciled": 0, "broker_rejected": 0})

    mcp.replace_order_by_id.assert_not_awaited()
    assert store._memory_orders[client_order_id]["status"] == "filled"
    assert await store.orders_in_flight() == []


async def test_reconcile_still_records_the_order_when_a_reprice_is_refused() -> None:
    """The order can settle between the read and the replace.

    Losing that race is not a reconcile failure: the status just read still
    has to reach the store, or the order never leaves the in-flight list.
    """
    settings = _reprice_settings()
    store, client_order_id = await _stale_close_order(settings)
    mcp = MagicMock()
    mcp.get_order_by_client_id = AsyncMock(
        return_value={
            "id": "broker-1",
            "status": "new",
            "filled_qty": "0",
            "limit_price": "2.00",
        }
    )
    mcp.replace_order_by_id = AsyncMock(
        side_effect=ToolError(
            "HTTP error 422: Unprocessable Entity - "
            "{'code': 42210000, 'message': 'order is not open'}"
        )
    )
    agent = ExecutionAgent(settings, mcp, store, MagicMock())
    detail: dict[str, Any] = {"reconciled": 0, "broker_rejected": 0}

    await agent._reconcile(detail)

    assert detail["reconciled"] == 1
    assert detail.get("repriced", 0) == 0
    assert store._memory_orders[client_order_id]["status"] == "new"
