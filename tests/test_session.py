"""The shared market-session gate, including the replay testing mode."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import options_m.__main__
from options_m import session
from options_m.config import Settings
from options_m.db import Database
from options_m.store import Store

# A Wednesday session and the Saturday that follows it.
_SESSION_DAY = datetime(2026, 8, 26, tzinfo=UTC).date()
_SESSION_OPEN = datetime(2026, 8, 26, 13, 30, tzinfo=UTC)
_SESSION_CLOSE = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
_WEEKEND = datetime(2026, 8, 29, 19, 0, tzinfo=UTC)
_MID_SESSION = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)


def _store(**overrides: object) -> tuple[Store, Settings]:
    settings = Settings(database_url=None, **overrides)  # type: ignore[arg-type]
    return Store(Database(settings)), settings


async def _with_session(**overrides: object) -> tuple[Store, Settings]:
    store, settings = _store(**overrides)
    await store.upsert_market_calendar(
        [
            {
                "date": _SESSION_DAY,
                "open": _SESSION_OPEN,
                "close": _SESSION_CLOSE,
                "session_type": "full",
            }
        ]
    )
    return store, settings


async def test_an_open_market_is_open_and_is_not_a_replay() -> None:
    store, settings = await _with_session(replay_last_session=True)

    state = await session.current(store, settings, _MID_SESSION)

    assert state.is_open is True
    assert state.replayed is False


async def test_a_closed_market_stays_closed_without_the_flag() -> None:
    store, settings = await _with_session(replay_last_session=False)

    state = await session.current(store, settings, _WEEKEND)

    assert state.is_open is False
    assert state.replayed is False


async def test_the_flag_replays_the_last_real_session_out_of_hours() -> None:
    store, settings = await _with_session(replay_last_session=True)

    state = await session.current(store, settings, _WEEKEND)

    assert state.is_open is True
    assert state.replayed is True


async def test_the_flag_cannot_fabricate_a_session_from_an_empty_calendar() -> None:
    """Replaying nothing must read as closed, not as open."""
    store, settings = _store(replay_last_session=True)

    state = await session.current(store, settings, _WEEKEND)

    assert state.is_open is False
    assert state.replayed is False


async def test_the_flag_does_not_replay_a_session_that_has_not_happened_yet() -> None:
    store, settings = await _with_session(replay_last_session=True)

    state = await session.current(store, settings, _SESSION_OPEN - timedelta(days=1))

    assert state.is_open is False


async def test_last_session_close_returns_the_in_progress_session_not_the_previous_one() -> None:
    store, _ = await _with_session()

    assert await store.last_session_close(_MID_SESSION) == _SESSION_CLOSE


def test_startup_refuses_to_replay_a_session_with_dry_run_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale evidence must never be allowed to reach order entry."""
    monkeypatch.setattr(
        options_m.__main__, "Settings", lambda: Settings(replay_last_session=True, dry_run=False)
    )
    monkeypatch.setattr(options_m.__main__, "assert_paper_intent", lambda: None)

    def _must_not_run(_settings: Settings) -> None:
        raise AssertionError("the interlock should have refused before starting anything")

    monkeypatch.setattr(asyncio, "run", _must_not_run)

    assert options_m.__main__.main() == 1
