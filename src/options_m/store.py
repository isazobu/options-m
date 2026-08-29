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
from options_m.volatility import iv_rank

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


def _maybe_float(value: object) -> float | None:
    """Coerce a stored numeric back to float. ``numeric`` columns read as Decimal."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class Store:
    """Repository over :class:`~options_m.db.Database`."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._memory_agent_runs: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_equity: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_snapshots: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_candidates: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_iv_history: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_kill_switch: tuple[bool, str | None] = (False, None)
        self._memory_proposals: dict[int, dict[str, Any]] = {}
        self._memory_proposal_seq = 0
        self._memory_orders: dict[str, dict[str, Any]] = {}
        self._memory_order_seq = 0
        self._memory_risk_events: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_iv_history: dict[str, deque[dict[str, Any]]] = {}
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

    async def append_iv_snapshot(
        self,
        symbol: str,
        *,
        iv_atm: float,
        dte: int | None = None,
        spot: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """One near-the-money implied-vol reading for ``symbol``.

        The evidence pack's IV Rank is this symbol's latest reading against its
        own recent history, so this needs to be written on a regular cadence
        (once per pull) for the rank to mean anything.
        """
        symbol = symbol.upper()
        if not self._db.is_enabled:
            self._memory_iv_history.appendleft(
                {
                    "ts": _now(),
                    "symbol": symbol,
                    "iv_atm": iv_atm,
                    "dte": dte,
                    "spot": spot,
                    "payload": payload,
                }
            )
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO iv_history (symbol, iv_atm, dte, spot, payload) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    symbol,
                    _as_decimal(iv_atm),
                    dte,
                    _as_decimal(spot),
                    json.dumps(payload) if payload else None,
                ),
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

    async def recent_iv(self, symbol: str, limit: int = 252) -> list[dict[str, Any]]:
        """This symbol's implied-vol readings, newest first."""
        symbol = symbol.upper()
        if not self._db.is_enabled:
            rows = [r for r in self._memory_iv_history if r["symbol"] == symbol]
            return rows[:limit]
        return await self._fetch(
            "SELECT ts, symbol, iv_atm, dte, spot "
            "FROM iv_history WHERE symbol = %s ORDER BY ts DESC LIMIT %s",
            (symbol, limit),
        )

    async def iv_rank_for(self, symbol: str, *, window: int = 252) -> float | None:
        """IV Rank of ``symbol``'s latest reading over its last ``window`` readings.

        ``None`` until at least two readings exist. This is the number the
        volatility analyst reasons about.
        """
        rows = await self.recent_iv(symbol, window)
        # recent_iv is newest-first; iv_rank wants chronological order.
        values = [_maybe_float(row.get("iv_atm")) for row in reversed(rows)]
        return iv_rank(values)

    async def recent_lessons(self, symbol: str | None = None, n: int = 3) -> list[str]:
        """Post-trade lessons for a symbol (or portfolio-wide when ``symbol`` is
        ``None``). Phase 4's reflection agent fills this; until then it is
        deliberately empty rather than absent, so callers can wire it now."""
        del symbol, n
        return []

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

    # ---- Proposals -----------------------------------------------------

    async def save_proposal(
        self, *, underlying: str, intent: dict[str, Any], evidence: dict[str, Any]
    ) -> int:
        """Insert a new pending proposal. Returns its id."""
        if not self._db.is_enabled:
            self._memory_proposal_seq += 1
            proposal_id = self._memory_proposal_seq
            self._memory_proposals[proposal_id] = {
                "id": proposal_id,
                "ts": _now(),
                "underlying": underlying,
                "status": "pending",
                "intent": intent,
                "evidence": evidence,
                "arguments": None,
                "verdict": None,
                "plan": None,
                "error": None,
            }
            return proposal_id
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO proposals (underlying, intent, evidence) "
                "VALUES (%s, %s, %s) RETURNING id",
                (underlying, json.dumps(intent), json.dumps(evidence)),
            )
            row = cast("tuple[Any, ...] | None", await cur.fetchone())
            await conn.commit()
            assert row is not None  # noqa: S101 - RETURNING always yields a row on success
            return int(row[0])

    async def pending_proposals(self, limit: int = 5) -> list[dict[str, Any]]:
        """Proposals awaiting execution, oldest first — a fair queue."""
        if not self._db.is_enabled:
            rows = [row for row in self._memory_proposals.values() if row["status"] == "pending"]
            rows.sort(key=lambda row: row["id"])
            return rows[:limit]
        return await self._fetch(
            "SELECT id, ts, underlying, status, intent, evidence, arguments, verdict, plan, error "
            "FROM proposals WHERE status = 'pending' ORDER BY ts ASC LIMIT %s",
            (limit,),
        )

    async def recent_proposals(
        self, limit: int = 50, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Most recent proposals, newest first — the decision-timeline feed.

        List-shaped on purpose: no ``evidence``/``intent`` blobs, so the
        payload stays small when listing many rows. Use :meth:`get_proposal`
        for the full detail of one.
        """
        if not self._db.is_enabled:
            rows = list(self._memory_proposals.values())
            if status is not None:
                rows = [row for row in rows if row["status"] == status]
            rows.sort(key=lambda row: row["ts"], reverse=True)
            return [
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "underlying": row["underlying"],
                    "status": row["status"],
                    "has_arguments": row["arguments"] is not None,
                    "has_verdict": row["verdict"] is not None,
                }
                for row in rows[:limit]
            ]
        if status is not None:
            return await self._fetch(
                "SELECT id, ts, underlying, status, "
                "(arguments IS NOT NULL) AS has_arguments, "
                "(verdict IS NOT NULL) AS has_verdict "
                "FROM proposals WHERE status = %s ORDER BY ts DESC LIMIT %s",
                (status, limit),
            )
        return await self._fetch(
            "SELECT id, ts, underlying, status, "
            "(arguments IS NOT NULL) AS has_arguments, "
            "(verdict IS NOT NULL) AS has_verdict "
            "FROM proposals ORDER BY ts DESC LIMIT %s",
            (limit,),
        )

    async def get_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        if not self._db.is_enabled:
            return self._memory_proposals.get(proposal_id)
        rows = await self._fetch(
            "SELECT id, ts, underlying, status, intent, evidence, arguments, verdict, plan, error "
            "FROM proposals WHERE id = %s",
            (proposal_id,),
        )
        return rows[0] if rows else None

    async def update_proposal_status(
        self,
        proposal_id: int,
        status: str,
        *,
        plan: dict[str, Any] | None = None,
        verdict: dict[str, Any] | None = None,
        arguments: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Partial update: only the fields passed are overwritten."""
        if not self._db.is_enabled:
            row = self._memory_proposals.get(proposal_id)
            if row is None:
                return
            row["status"] = status
            if plan is not None:
                row["plan"] = plan
            if verdict is not None:
                row["verdict"] = verdict
            if arguments is not None:
                row["arguments"] = arguments
            if error is not None:
                row["error"] = error
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE proposals SET status = %s, "
                "plan = COALESCE(%s, plan), verdict = COALESCE(%s, verdict), "
                "arguments = COALESCE(%s, arguments), error = COALESCE(%s, error) "
                "WHERE id = %s",
                (
                    status,
                    json.dumps(plan) if plan is not None else None,
                    json.dumps(verdict) if verdict is not None else None,
                    json.dumps(arguments) if arguments is not None else None,
                    error,
                    proposal_id,
                ),
            )
            await conn.commit()

    # ---- Orders ----------------------------------------------------------

    async def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        if not self._db.is_enabled:
            return self._memory_orders.get(client_order_id)
        rows = await self._fetch(
            "SELECT id, proposal_id, client_order_id, submitted_at, status, request, "
            "response, filled_qty, filled_avg_price, error FROM orders WHERE client_order_id = %s",
            (client_order_id,),
        )
        return rows[0] if rows else None

    async def record_order(
        self,
        *,
        proposal_id: int,
        client_order_id: str,
        status: str,
        request: dict[str, Any],
        response: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """First write for one order attempt.

        A duplicate ``client_order_id`` here means the caller is retrying an
        already-recorded attempt, so it is a no-op rather than an overwrite —
        status transitions go through :meth:`update_order_status` instead.
        """
        if not self._db.is_enabled:
            if client_order_id in self._memory_orders:
                return
            self._memory_order_seq += 1
            self._memory_orders[client_order_id] = {
                "id": self._memory_order_seq,
                "proposal_id": proposal_id,
                "client_order_id": client_order_id,
                "submitted_at": _now(),
                "status": status,
                "request": request,
                "response": response,
                "filled_qty": None,
                "filled_avg_price": None,
                "error": error,
            }
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO orders "
                "(proposal_id, client_order_id, status, request, response, error) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (client_order_id) DO NOTHING",
                (
                    proposal_id,
                    client_order_id,
                    status,
                    json.dumps(request),
                    json.dumps(response) if response is not None else None,
                    error,
                ),
            )
            await conn.commit()

    async def update_order_status(
        self,
        client_order_id: str,
        *,
        status: str,
        response: dict[str, Any] | None = None,
        filled_qty: float | None = None,
        filled_avg_price: float | None = None,
        error: str | None = None,
    ) -> None:
        """Used by reconciliation and by the duplicate-as-success path."""
        if not self._db.is_enabled:
            row = self._memory_orders.get(client_order_id)
            if row is None:
                return
            row["status"] = status
            if response is not None:
                row["response"] = response
            if filled_qty is not None:
                row["filled_qty"] = filled_qty
            if filled_avg_price is not None:
                row["filled_avg_price"] = filled_avg_price
            if error is not None:
                row["error"] = error
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE orders SET status = %s, response = COALESCE(%s, response), "
                "filled_qty = COALESCE(%s, filled_qty), "
                "filled_avg_price = COALESCE(%s, filled_avg_price), "
                "error = COALESCE(%s, error) WHERE client_order_id = %s",
                (
                    status,
                    json.dumps(response) if response is not None else None,
                    _as_decimal(filled_qty),
                    _as_decimal(filled_avg_price),
                    error,
                    client_order_id,
                ),
            )
            await conn.commit()

    async def orders_for_proposal(self, proposal_id: int) -> list[dict[str, Any]]:
        """Every order attempt tied to one proposal, oldest first."""
        if not self._db.is_enabled:
            rows = [
                row for row in self._memory_orders.values() if row["proposal_id"] == proposal_id
            ]
            rows.sort(key=lambda row: row["submitted_at"])
            return rows
        return await self._fetch(
            "SELECT id, proposal_id, client_order_id, submitted_at, status, request, "
            "response, filled_qty, filled_avg_price, error FROM orders "
            "WHERE proposal_id = %s ORDER BY submitted_at ASC",
            (proposal_id,),
        )

    async def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent order attempts across every proposal, newest first."""
        if not self._db.is_enabled:
            rows = sorted(
                self._memory_orders.values(), key=lambda row: row["submitted_at"], reverse=True
            )
            return rows[:limit]
        return await self._fetch(
            "SELECT id, proposal_id, client_order_id, submitted_at, status, request, "
            "response, filled_qty, filled_avg_price, error FROM orders "
            "ORDER BY submitted_at DESC LIMIT %s",
            (limit,),
        )

    async def orders_in_flight(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self._db.is_enabled:
            rows = [row for row in self._memory_orders.values() if row["status"] == "submitted"]
            return rows[:limit]
        return await self._fetch(
            "SELECT id, proposal_id, client_order_id, submitted_at, status, request, "
            "response, filled_qty, filled_avg_price, error FROM orders "
            "WHERE status = 'submitted' ORDER BY submitted_at ASC LIMIT %s",
            (limit,),
        )

    # ---- Risk events -------------------------------------------------------

    async def record_risk_event(
        self, *, proposal_id: int | None, rule: str, detail: dict[str, Any]
    ) -> None:
        row = {"ts": _now(), "proposal_id": proposal_id, "rule": rule, "detail": detail}
        if not self._db.is_enabled:
            self._memory_risk_events.appendleft(row)
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO risk_events (proposal_id, rule, detail) VALUES (%s, %s, %s)",
                (proposal_id, rule, json.dumps(detail)),
            )
            await conn.commit()

    async def recent_risk_events(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self._db.is_enabled:
            return list(self._memory_risk_events)[:limit]
        return await self._fetch(
            "SELECT ts, proposal_id, rule, detail FROM risk_events ORDER BY ts DESC LIMIT %s",
            (limit,),
        )

    # ---- IV history ----------------------------------------------------

    async def save_iv_history(
        self,
        *,
        symbol: str,
        iv_atm: float | None,
        put_call_skew: float | None,
        term_structure: float | None,
        median_spread_pct: float | None,
        total_open_interest: int | None,
    ) -> None:
        row = {
            "ts": _now(),
            "symbol": symbol,
            "iv_atm": iv_atm,
            "put_call_skew": put_call_skew,
            "term_structure": term_structure,
            "median_spread_pct": median_spread_pct,
            "total_open_interest": total_open_interest,
        }
        if not self._db.is_enabled:
            self._memory_iv_history.setdefault(symbol, deque(maxlen=_MEMORY_LIMIT)).appendleft(row)
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO iv_history (symbol, iv_atm, put_call_skew, term_structure, "
                "median_spread_pct, total_open_interest) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    symbol,
                    _as_decimal(iv_atm),
                    _as_decimal(put_call_skew),
                    _as_decimal(term_structure),
                    _as_decimal(median_spread_pct),
                    total_open_interest,
                ),
            )
            await conn.commit()

    async def recent_iv(self, symbol: str, n: int = 20) -> list[dict[str, Any]]:
        if not self._db.is_enabled:
            return list(self._memory_iv_history.get(symbol, ()))[:n]
        return await self._fetch(
            "SELECT ts, iv_atm, put_call_skew, term_structure, median_spread_pct, "
            "total_open_interest FROM iv_history WHERE symbol = %s ORDER BY ts DESC LIMIT %s",
            (symbol, n),
        )

    async def recent_lessons(self, symbol: str, n: int = 5) -> list[dict[str, Any]]:
        """Phase 4 fills this in with a real ``lessons`` table.

        Returns an empty list unconditionally so evidence.py's shape is stable
        now and does not need to change again when Phase 4 lands.
        """
        return []

    async def _fetch(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            columns = [column.name for column in cur.description or []]
            rows = cast("list[tuple[Any, ...]]", await cur.fetchall())
            return [dict(zip(columns, row, strict=True)) for row in rows]
