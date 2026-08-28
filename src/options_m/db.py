"""Postgres connection pool.

Thin wrapper around ``psycopg_pool`` so the rest of the app never touches
connection lifecycle directly. No queries live here — those belong with the
code that owns the data.
"""

from __future__ import annotations

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
            open=False,
        )
        await pool.open(wait=True, timeout=self._timeout)
        self._pool = pool
        logger.info(
            "database pool ready",
            extra={"min_size": self._min_size, "max_size": self._max_size},
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
        """Return whether a trivial query succeeds. Never raises."""
        if self._pool is None:
            return False
        try:
            async with self.connection() as conn, conn.cursor() as cur:
                await cur.execute("SELECT 1")
        except Exception:
            logger.exception("database ping failed")
            return False
        return True

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
