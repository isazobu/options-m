"""Tests for the in-memory fallback path of the store.

Postgres itself is exercised against Neon in the live run; what matters here is
that a developer with no database gets a working service rather than a crash,
and that the fallback keeps the same contract as the persistent path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

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


async def test_iv_history_is_per_symbol_and_newest_first() -> None:
    store = _store()
    for iv in (0.20, 0.25, 0.30):
        await store.append_iv_snapshot("aapl", iv_atm=iv, dte=30)
    await store.append_iv_snapshot("MSFT", iv_atm=0.5)

    rows = await store.recent_iv("AAPL")

    assert [row["iv_atm"] for row in rows] == [0.30, 0.25, 0.20]
    assert all(row["symbol"] == "AAPL" for row in rows)


def _session_close(day: date) -> datetime:
    """16:00 New York on ``day``, as UTC — where a daily reading is dated."""
    return datetime.combine(day, time(hour=16), tzinfo=ZoneInfo("America/New_York")).astimezone(
        UTC
    )


async def _fill_sessions(store: Store, symbol: str, ivs: list[float]) -> None:
    """One reading per consecutive calendar day, oldest first."""
    start = date(2026, 1, 5)
    for offset, iv in enumerate(ivs):
        await store.append_iv_snapshot(
            symbol, iv_atm=iv, dte=30, ts=_session_close(start + timedelta(days=offset))
        )


async def test_iv_rank_for_ranks_the_latest_session_against_the_year() -> None:
    store = _store()
    # Ascending across enough sessions to clear the minimum -> latest is the
    # window maximum -> rank 100.
    await _fill_sessions(store, "SPY", [0.10 + index * 0.001 for index in range(130)])

    assert await store.iv_rank_for("SPY") == 100.0


async def test_iv_rank_is_none_until_the_window_holds_enough_sessions() -> None:
    """The bug this replaced: 252 *readings* at a one-minute pulse is 252
    minutes, so a rank appeared within hours of a cold start and swung tens of
    points per tick. A rank is a daily statistic over a trading year."""
    store = _store()
    assert await store.iv_rank_for("SPY") is None

    # A full session of minute-by-minute readings is still one observation.
    opened = _session_close(date(2026, 1, 5)) - timedelta(hours=6)
    for minute in range(400):
        await store.append_iv_snapshot(
            "SPY", iv_atm=0.20 + minute * 0.0005, ts=opened + timedelta(minutes=minute)
        )

    assert len(await store.daily_iv_history("SPY")) == 1
    assert await store.iv_rank_for("SPY") is None
    rank, pctile, sessions = await store.iv_rank_and_percentile("SPY")
    assert (rank, pctile, sessions) == (None, None, 1)


async def test_the_daily_series_keeps_each_session_s_last_reading() -> None:
    store = _store()
    day = date(2026, 1, 5)
    close = _session_close(day)
    for offset, iv in ((-120, 0.21), (-60, 0.25), (0, 0.29)):
        await store.append_iv_snapshot(
            "SPY", iv_atm=iv, ts=close + timedelta(minutes=offset)
        )
    await store.append_iv_snapshot("SPY", iv_atm=0.40, ts=_session_close(day + timedelta(days=1)))

    rows = await store.daily_iv_history("SPY")

    # Newest session first, and each session represented by its close, not its
    # open and not its intraday extreme.
    assert [row["iv_atm"] for row in rows] == [0.40, 0.29]
    assert [row["session_day"] for row in rows] == [day + timedelta(days=1), day]


async def test_iv_rank_min_days_is_counted_in_sessions_not_readings() -> None:
    store = _store()
    await _fill_sessions(store, "SPY", [0.20 + index * 0.001 for index in range(5)])

    # Five sessions is far short of the default floor.
    assert await store.iv_rank_for("SPY") is None
    # Asked over a window it does satisfy, the same data ranks.
    assert await store.iv_rank_for("SPY", days=252, min_days=5) == 100.0


async def test_a_reading_with_no_iv_is_not_an_observation() -> None:
    """An IV that would not solve must not enter the window: as a null it would
    be dropped by the rank anyway, but as a *session* it would count toward the
    minimum and pass the gate on data that is not there."""
    store = _store()
    await store.append_iv_snapshot(
        "SPY",
        iv_atm=None,  # type: ignore[arg-type]
        ts=_session_close(date(2026, 1, 5)),
    )

    assert await store.daily_iv_history("SPY") == []
    assert await store.iv_rank_and_percentile("SPY") == (None, None, 0)


async def test_iv_sessions_report_which_days_are_already_covered() -> None:
    """What the backfill reads to stay incremental."""
    store = _store()
    await _fill_sessions(store, "SPY", [0.20, 0.21, 0.22])

    assert await store.iv_session_days("SPY") == {
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
    }


async def test_backfilled_readings_land_on_their_own_sessions() -> None:
    store = _store()
    written = await store.append_iv_snapshots(
        "spy",
        [
            {"ts": _session_close(date(2026, 1, 5)), "iv_atm": 0.30, "dte": 30, "spot": 100.0},
            {"ts": _session_close(date(2026, 1, 6)), "iv_atm": 0.20, "dte": 29, "spot": 101.0},
        ],
    )

    assert written == 2
    rows = await store.daily_iv_history("SPY")
    assert [row["iv_atm"] for row in rows] == [0.20, 0.30]  # newest session first
    assert await store.iv_rank_for("SPY", min_days=2) == 0.0  # latest is the window low


async def test_recent_lessons_is_empty_until_phase_four() -> None:
    store = _store()
    assert await store.recent_lessons("SPY") == []
    assert await store.recent_lessons(None) == []


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


async def test_active_proposal_underlyings_counts_every_slot_holding_status() -> None:
    """pending / dry_run_approved / submitted each hold a position slot; a
    rejected or no_action proposal does not."""
    store = _store()
    pending_id = await store.save_proposal(underlying="SPY", intent={}, evidence={})
    approved_id = await store.save_proposal(underlying="QQQ", intent={}, evidence={})
    await store.update_proposal_status(approved_id, "dry_run_approved")
    submitted_id = await store.save_proposal(underlying="AAPL", intent={}, evidence={})
    await store.update_proposal_status(submitted_id, "submitted")
    rejected_id = await store.save_proposal(underlying="TSLA", intent={}, evidence={})
    await store.update_proposal_status(rejected_id, "rejected")
    held_id = await store.save_proposal(underlying="META", intent={}, evidence={})
    await store.update_proposal_status(held_id, "no_action")

    assert await store.active_proposal_underlyings() == {"SPY", "QQQ", "AAPL"}
    # The proposal being executed must not block itself.
    assert await store.active_proposal_underlyings(exclude_proposal_id=pending_id) == {
        "QQQ",
        "AAPL",
    }


async def test_working_order_underlyings_parses_single_and_multi_leg_requests() -> None:
    store = _store()
    await store.record_order(
        proposal_id=1,
        client_order_id="om-1",
        status="submitted",
        request={"symbol": "AAPL250620C00190000", "qty": "1"},
    )
    await store.record_order(
        proposal_id=2,
        client_order_id="om-2",
        status="submitted",
        request={
            "legs": [
                {"symbol": "SPY250620P00500000"},
                {"symbol": "SPY250620P00490000"},
            ]
        },
    )
    # A filled order no longer rests at the broker.
    await store.record_order(
        proposal_id=3,
        client_order_id="om-3",
        status="filled",
        request={"symbol": "TSLA250620C00250000"},
    )

    assert await store.working_order_underlyings() == {"AAPL", "SPY"}


async def test_orders_in_flight_is_a_denylist_not_a_submitted_match() -> None:
    """The first reconcile tick overwrites the local 'submitted' with the
    broker status; matching only 'submitted' would drop an 'accepted' order
    from reconcile forever."""
    store = _store()
    for cid, status in (
        ("om-sub", "submitted"),
        ("om-acc", "accepted"),
        ("om-part", "partially_filled"),
        ("om-fill", "filled"),
        ("om-rej", "rejected"),
        ("om-can", "canceled"),
        ("om-fail", "failed"),
    ):
        await store.record_order(
            proposal_id=1, client_order_id=cid, status=status, request={}
        )

    in_flight = {row["client_order_id"] for row in await store.orders_in_flight()}

    assert in_flight == {"om-sub", "om-acc", "om-part"}


async def test_proposals_since_returns_status_and_respects_the_window() -> None:
    store = _store()
    old_id = await store.save_proposal(underlying="SPY", intent={}, evidence={})
    store._memory_proposals[old_id]["ts"] = datetime.now(UTC) - timedelta(days=2)
    recent_id = await store.save_proposal(underlying="QQQ", intent={}, evidence={})
    await store.update_proposal_status(recent_id, "rejected")

    rows = await store.proposals_since(datetime.now(UTC) - timedelta(days=1))

    assert [(row["underlying"], row["status"]) for row in rows] == [("QQQ", "rejected")]


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
