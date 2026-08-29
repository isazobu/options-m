"""Persistence for everything the agents decide.

All SQL in the codebase lives here. Two reasons: the audit trail is the point of
this system — a judge must be able to replay any decision after the fact — and
keeping queries in one module makes it obvious what the service actually writes.

When ``DATABASE_URL`` is unset the store falls back to bounded in-memory
deques so local development and the whole test suite run with no Postgres. The
fallback is announced loudly at startup; silently pretending to persist would be
its own kind of lie.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from options_m.db import Database

logger = logging.getLogger(__name__)

# Enough history for the dashboard without letting a long run eat the heap.
_MEMORY_LIMIT = 2_000


def _now() -> datetime:
    return datetime.now(UTC)


def _as_decimal(value: float | None) -> Decimal | None:
    """Money crosses into Postgres as numeric, never as a float."""
    if value is None:
        return None
    return Decimal(str(value))


class Store:
    """Repository over :class:`~options_m.db.Database`."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._memory_agent_runs: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_equity: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_snapshots: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_candidates: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_kill_switch: tuple[bool, str | None] = (False, None)
        if not db.is_enabled:
            logger.warning(
                "no database configured; the store is keeping the last %d rows per table "
                "in memory only. Nothing is persisted across restarts.",
                _MEMORY_LIMIT,
            )

    @property
    def is_persistent(self) -> bool:
        return self._db.is_enabled

    async def record_agent_run(
        self,
        agent: str,
        *,
        duration_ms: int,
        ok: bool,
        error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Telemetry for one agent iteration. Drives the agent-health panel."""
        row = {
            "agent": agent,
            "started_at": _now(),
            "duration_ms": duration_ms,
            "ok": ok,
            "error": error,
            "detail": detail,
        }
        if not self._db.is_enabled:
            self._memory_agent_runs.appendleft(row)
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO agent_runs (agent, duration_ms, ok, error, detail) "
                "VALUES (%s, %s, %s, %s, %s)",
                (agent, duration_ms, ok, error, json.dumps(detail) if detail else None),
            )
            await conn.commit()

    async def append_equity(
        self,
        *,
        equity: float | None,
        cash: float | None,
        buying_power: float | None,
        positions_count: int,
    ) -> None:
        """One point on the equity curve.

        Every value is nullable on purpose: a broker field we could not read is
        recorded as unknown, never as zero.
        """
        row = {
            "ts": _now(),
            "equity": equity,
            "cash": cash,
            "buying_power": buying_power,
            "positions_count": positions_count,
        }
        if not self._db.is_enabled:
            self._memory_equity.appendleft(row)
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO equity_curve (equity, cash, buying_power, positions_count) "
                "VALUES (%s, %s, %s, %s)",
                (
                    _as_decimal(equity),
                    _as_decimal(cash),
                    _as_decimal(buying_power),
                    positions_count,
                ),
            )
            await conn.commit()

    async def save_market_snapshot(self, payload: dict[str, Any]) -> None:
        if not self._db.is_enabled:
            self._memory_snapshots.appendleft({"ts": _now(), "payload": payload})
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO market_snapshots (payload) VALUES (%s)",
                (json.dumps(payload),),
            )
            await conn.commit()

    async def save_candidates(self, candidates: list[dict[str, Any]]) -> None:
        """Persist a whole candidate set in one round trip.

        Neon bills compute time and every touch keeps it awake, so this takes an
        entire batch. Do not call it once per symbol inside a loop.
        """
        if not candidates:
            return
        if not self._db.is_enabled:
            for candidate in candidates:
                self._memory_candidates.appendleft({"ts": _now(), **candidate})
            return
        rows = [
            (
                candidate["symbol"],
                candidate.get("reason"),
                _as_decimal(candidate.get("score")),
                json.dumps(candidate.get("payload") or {}),
            )
            for candidate in candidates
        ]
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO candidates (symbol, reason, score, payload) VALUES (%s, %s, %s, %s)",
                rows,
            )
            await conn.commit()

    async def recent_agent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self._db.is_enabled:
            return list(self._memory_agent_runs)[:limit]
        return await self._fetch(
            "SELECT agent, started_at, duration_ms, ok, error, detail "
            "FROM agent_runs ORDER BY started_at DESC LIMIT %s",
            (limit,),
        )

    async def recent_equity(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self._db.is_enabled:
            return list(self._memory_equity)[:limit]
        return await self._fetch(
            "SELECT ts, equity, cash, buying_power, positions_count "
            "FROM equity_curve ORDER BY ts DESC LIMIT %s",
            (limit,),
        )

    async def recent_candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self._db.is_enabled:
            return list(self._memory_candidates)[:limit]
        return await self._fetch(
            "SELECT ts, symbol, reason, score, payload "
            "FROM candidates ORDER BY ts DESC LIMIT %s",
            (limit,),
        )

    async def is_kill_switch_engaged(self) -> bool:
        if not self._db.is_enabled:
            return self._memory_kill_switch[0]
        rows = await self._fetch("SELECT engaged FROM kill_switch WHERE id = 1", ())
        if not rows:
            return False
        return bool(rows[0]["engaged"])

    async def set_kill_switch(self, engaged: bool, reason: str | None = None) -> None:
        if not self._db.is_enabled:
            self._memory_kill_switch = (engaged, reason)
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO kill_switch (id, engaged, reason, updated_at) "
                "VALUES (1, %s, %s, now()) "
                "ON CONFLICT (id) DO UPDATE SET engaged = EXCLUDED.engaged, "
                "reason = EXCLUDED.reason, updated_at = now()",
                (engaged, reason),
            )
            await conn.commit()

    async def _fetch(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            columns = [column.name for column in cur.description or []]
            rows = cast("list[tuple[Any, ...]]", await cur.fetchall())
            return [dict(zip(columns, row, strict=True)) for row in rows]
