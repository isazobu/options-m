from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from options_m.config import Settings
from options_m.db import Database


def test_database_is_disabled_without_a_dsn() -> None:
    db = Database(Settings(database_url=None))

    assert db.is_enabled is False


def test_database_is_enabled_with_a_dsn() -> None:
    db = Database(Settings(database_url="postgresql://user:pass@host/db"))

    assert db.is_enabled is True


async def test_ping_is_false_when_the_pool_was_never_opened() -> None:
    db = Database(Settings(database_url="postgresql://user:pass@host/db"))

    assert await db.ping() is False


async def test_ping_gives_up_instead_of_hanging() -> None:
    """A hanging readiness probe is worse than a failing one: the platform's
    own health check times out rather than seeing a 503."""
    db = Database(Settings(database_url="postgresql://u:p@host/db", db_ping_timeout_seconds=0.2))

    @asynccontextmanager
    async def _never_connects() -> AsyncIterator[object]:
        await asyncio.sleep(30)  # An unreachable host behaves like this.
        yield object()  # pragma: no cover

    # A non-None pool is what makes ping() attempt a connection at all.
    db._pool = object()  # type: ignore[assignment]
    db.connection = _never_connects  # type: ignore[method-assign, assignment]

    started = time.monotonic()
    result = await db.ping()
    elapsed = time.monotonic() - started

    assert result is False
    assert elapsed < 2.0, f"ping hung for {elapsed:.1f}s instead of timing out"
