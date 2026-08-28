"""Postgres connection pool.

Thin wrapper around ``psycopg_pool`` so the rest of the app never touches
connection lifecycle directly. No queries live here — those belong with the
code that owns the data.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Self

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from options_m.config import Settings

logger = logging.getLogger(__name__)


class Database:
    """Lazily-opened Postgres pool that tolerates being unconfigured.

    When ``DATABASE_URL`` is unset the pool is never created and
    :meth:`is_enabled` reports ``False``, so the app still boots locally.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.database_url
        self._min_size = settings.db_pool_min_size
        self._max_size = settings.db_pool_max_size
        self._timeout = settings.db_connect_timeout_seconds
        self._max_idle = settings.db_pool_max_idle_seconds
        self._ping_timeout = settings.db_ping_timeout_seconds
        self._pool: AsyncConnectionPool[AsyncConnection[object]] | None = None

    @property
    def is_enabled(self) -> bool:
        return self._dsn is not None

    async def connect(self) -> None:
        """Open the pool. No-op when no DSN is configured."""
        if self._dsn is None:
            logger.warning("DATABASE_URL is not set; running without a database")
            return

        pool: AsyncConnectionPool[AsyncConnection[object]] = AsyncConnectionPool(
            conninfo=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            # Serverless Postgres (Neon and friends) drops idle connections when
            # it scales its compute to zero. Without this check a stale pooled
            # connection would surface as an error on the next query.
            check=AsyncConnectionPool.check_connection,
            max_idle=self._max_idle,
            open=False,
        )
        # wait=False keeps startup independent of database availability: with
        # min_size=0 there is nothing to pre-open, and a transient outage must
        # not turn into a container crash-loop.
        await pool.open(wait=self._min_size > 0, timeout=self._timeout)
        self._pool = pool
        logger.info(
            "database pool ready",
            extra={
                "min_size": self._min_size,
                "max_size": self._max_size,
                "max_idle_seconds": self._max_idle,
            },
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("database pool closed")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection[object]]:
        """Borrow a connection from the pool.

        Raises:
            RuntimeError: If the database is not configured or not yet open.
        """
        if self._pool is None:
            msg = "database is not configured; set DATABASE_URL"
            raise RuntimeError(msg)
        async with self._pool.connection() as conn:
            yield conn

    async def ping(self) -> bool:
        """Return whether a trivial query succeeds. Never raises, never hangs.

        Bounded by ``db_ping_timeout_seconds``: when the database is
        unreachable, borrowing a connection would otherwise block for the
        pool's full timeout and leave the readiness probe hanging.
        """
        if self._pool is None:
            return False
        try:
            async with asyncio.timeout(self._ping_timeout):
                async with self.connection() as conn, conn.cursor() as cur:
                    await cur.execute("SELECT 1")
        except TimeoutError:
            logger.warning("database ping timed out", extra={"timeout_seconds": self._ping_timeout})
            return False
        except Exception:
            logger.exception("database ping failed")
            return False
        return True

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
