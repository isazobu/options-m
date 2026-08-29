"""Tests for the in-memory fallback path of the store.

Postgres itself is exercised against Neon in the live run; what matters here is
that a developer with no database gets a working service rather than a crash,
and that the fallback keeps the same contract as the persistent path.
"""

from __future__ import annotations

from datetime import UTC, datetime

from options_m.config import Settings
from options_m.db import Database
from options_m.store import Store


def _store() -> Store:
    return Store(Database(Settings(database_url=None)))


async def test_store_reports_that_it_is_not_persisting() -> None:
    """The fallback is announced, never silent."""
    assert _store().is_persistent is False


async def test_agent_runs_are_returned_newest_first() -> None:
    store = _store()
    for index in range(3):
        await store.record_agent_run(
            "market_pulse", duration_ms=index, ok=True, detail={"i": index}
        )

    runs = await store.recent_agent_runs()

    assert [run["duration_ms"] for run in runs] == [2, 1, 0]
    assert runs[0]["agent"] == "market_pulse"


async def test_a_failed_run_records_its_error() -> None:
    store = _store()
    await store.record_agent_run("market_pulse", duration_ms=5, ok=False, error="boom")

    run = (await store.recent_agent_runs())[0]

    assert run["ok"] is False
    assert run["error"] == "boom"


async def test_unavailable_equity_values_stay_none() -> None:
    """An unreadable broker field is recorded as unknown, never as zero."""
    store = _store()
    await store.append_equity(equity=None, cash=None, buying_power=None, positions_count=0)

    point = (await store.recent_equity())[0]

    assert point["equity"] is None
    assert point["cash"] is None


async def test_equity_points_accumulate_in_order() -> None:
    store = _store()
    for value in (100_000.0, 100_500.0):
        await store.append_equity(
            equity=value, cash=value, buying_power=value * 2, positions_count=1
        )

    points = await store.recent_equity()

    assert [point["equity"] for point in points] == [100_500.0, 100_000.0]


async def test_candidates_are_saved_as_a_batch() -> None:
    store = _store()
    await store.save_candidates(
        [
            {"symbol": "SPY", "score": 3.0, "reason": "moved", "payload": {}},
            {"symbol": "QQQ", "score": 1.0, "reason": "active", "payload": {}},
        ]
    )

    saved = await store.recent_candidates()

    assert {row["symbol"] for row in saved} == {"SPY", "QQQ"}


async def test_saving_no_candidates_is_a_no_op() -> None:
    store = _store()
    await store.save_candidates([])

    assert await store.recent_candidates() == []


async def test_kill_switch_round_trips() -> None:
    store = _store()
    assert await store.is_kill_switch_engaged() is False

    await store.set_kill_switch(True, reason="manual halt")
    assert await store.is_kill_switch_engaged() is True

    await store.set_kill_switch(False)
    assert await store.is_kill_switch_engaged() is False


async def test_recent_proposals_are_returned_newest_first_without_the_blobs() -> None:
    store = _store()
    for index, underlying in enumerate(("SPY", "QQQ", "AAPL")):
        proposal_id = await store.save_proposal(
            underlying=underlying, intent={"direction": "long"}, evidence={}
        )
        # Force distinct, ordered timestamps: three calls in one test can land
        # on the same microsecond, which would otherwise make ordering flaky.
        store._memory_proposals[proposal_id]["ts"] = datetime(2026, 1, index + 1, tzinfo=UTC)

    rows = await store.recent_proposals()

    assert [row["underlying"] for row in rows] == ["AAPL", "QQQ", "SPY"]
    assert rows[0]["has_arguments"] is False
    assert rows[0]["has_verdict"] is False
    assert rows[0]["is_mock"] is False
    assert "evidence" not in rows[0]


async def test_recent_proposals_flags_seeded_mock_rows() -> None:
    """The dashboard must never let seeded demo data read as a real decision."""
    store = _store()
    await store.save_proposal(underlying="SPY", intent={}, evidence={"mock": True})

    row = (await store.recent_proposals())[0]

    assert row["is_mock"] is True


async def test_recent_proposals_filters_by_status() -> None:
    store = _store()
    approved_id = await store.save_proposal(underlying="SPY", intent={}, evidence={})
    await store.update_proposal_status(approved_id, "approved")
    await store.save_proposal(underlying="QQQ", intent={}, evidence={})

    rows = await store.recent_proposals(status="approved")

    assert [row["underlying"] for row in rows] == ["SPY"]


async def test_recent_proposals_reflect_arguments_and_verdict_once_set() -> None:
    store = _store()
    proposal_id = await store.save_proposal(underlying="SPY", intent={}, evidence={})
    await store.update_proposal_status(
        proposal_id, "approved", arguments={"bull": "..."}, verdict={"action": "open"}
    )

    row = (await store.recent_proposals())[0]

    assert row["has_arguments"] is True
    assert row["has_verdict"] is True


async def test_orders_for_proposal_returns_only_that_proposals_orders() -> None:
    store = _store()
    proposal_id = await store.save_proposal(underlying="SPY", intent={}, evidence={})
    other_id = await store.save_proposal(underlying="QQQ", intent={}, evidence={})
    await store.record_order(
        proposal_id=proposal_id, client_order_id="om-1", status="submitted", request={}
    )
    await store.record_order(
        proposal_id=other_id, client_order_id="om-2", status="submitted", request={}
    )

    orders = await store.orders_for_proposal(proposal_id)

    assert [order["client_order_id"] for order in orders] == ["om-1"]


async def test_recent_orders_are_returned_newest_first() -> None:
    store = _store()
    proposal_id = await store.save_proposal(underlying="SPY", intent={}, evidence={})
    for index in range(2):
        await store.record_order(
            proposal_id=proposal_id,
            client_order_id=f"om-{index}",
            status="submitted",
            request={},
        )
    # Force distinct, ordered timestamps: two calls in the same test can land
    # on the same microsecond, which would otherwise make "newest first" flaky.
    store._memory_orders["om-0"]["submitted_at"] = datetime(2026, 1, 1, tzinfo=UTC)
    store._memory_orders["om-1"]["submitted_at"] = datetime(2026, 1, 2, tzinfo=UTC)

    orders = await store.recent_orders()

    assert [order["client_order_id"] for order in orders] == ["om-1", "om-0"]
