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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

from options_m.db import Database
from options_m.evidence.occ import parse_occ_symbol
from options_m.volatility import iv_percentile, iv_rank

logger = logging.getLogger(__name__)

# Enough history for the dashboard without letting a long run eat the heap.
_MEMORY_LIMIT = 2_000

# A proposal in one of these states stands for a position that
# get_all_positions cannot show yet: pending execution, dry-run approved, or
# submitted as a working order that has not filled. Candidate selection and
# the portfolio snapshot both count these alongside filled positions so the
# same symbol is not re-proposed while its first order is still resting.
_ACTIVE_PROPOSAL_STATUSES = ("pending", "dry_run_approved", "submitted")

# Order statuses that will not change on their own — the broker has finished
# with the order (``filled`` / ``canceled`` / ``rejected`` / …) or submission
# never reached it (``failed``). Anything else is still in flight. Compared
# case-insensitively so a broker "CANCELED" and our lowercase spelling match.
# Kept aligned with execution._TERMINAL_BROKER_STATES.
_SETTLED_ORDER_STATES = frozenset(
    {
        "filled",
        "canceled",
        "cancelled",
        "expired",
        "rejected",
        "replaced",
        "done_for_day",
        "failed",
    }
)

# The exchange's own calendar is expressed in its local time; every
# open/close boundary check must happen in this zone, never in UTC directly,
# or a late-evening UTC timestamp can resolve to the wrong agents day.
_EXCHANGE_TZ = ZoneInfo("America/New_York")

# IV Rank is a *daily* statistic over a *trading year* — the tastytrade
# definition every options desk quotes: where today's ATM IV sits between the
# highest and lowest daily ATM IV of the last 252 sessions. Both numbers below
# are counted in trading days, never in readings: MarketPulseAgent appends a
# reading every minute, so ranking the last 252 *rows* ranks the last four
# hours and calls the answer a year. See daily_iv_history.
IV_RANK_WINDOW_DAYS = 252
# Below this many distinct sessions the window is not a vol regime, and the
# honest answer is MISSING. Six months is the shortest span over which a rank
# still separates a quiet tape from a stressed one.
IV_RANK_MIN_DAYS = 126


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


def _session_day(ts: datetime) -> date:
    """The exchange session a timestamp belongs to.

    A US options session runs 09:30-16:00 New York, which never crosses a UTC
    midnight — but the pre-open and post-close writes do, so the day is taken
    in the exchange's zone rather than UTC.
    """
    return ts.astimezone(_EXCHANGE_TZ).date()


def _calendar_span_for(trading_days: int) -> int:
    """Calendar days that comfortably contain ``trading_days`` sessions.

    Weekends and holidays mean ~252 sessions span ~365 calendar days. Sized
    generously (the same idiom as AlpacaMcp.get_stock_bars) so the SQL time
    bound only exists to keep the scan off the index's tail — the row LIMIT,
    not this, is what caps the window.
    """
    return int(trading_days * 1.6) + 15


class Store:
    """Repository over :class:`~options_m.db.Database`."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._memory_agent_runs: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_equity: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_snapshots: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_candidates: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        # Per-symbol deques: _memory_iv_history[symbol] → deque of readings.
        self._memory_iv_history: dict[str, deque[dict[str, Any]]] = {}
        self._memory_kill_switch: tuple[bool, str | None] = (False, None)
        self._memory_proposals: dict[int, dict[str, Any]] = {}
        self._memory_proposal_seq = 0
        self._memory_orders: dict[str, dict[str, Any]] = {}
        self._memory_order_seq = 0
        self._memory_risk_events: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        self._memory_calendar: dict[date, dict[str, Any]] = {}
        self._memory_account: dict[str, Any] | None = None
        self._memory_positions: dict[str, dict[str, Any]] = {}
        # Evidence cache: sole writer MarketPulseAgent, sole reader StrategistAgent.
        self._memory_evidence: dict[str, dict[str, Any]] = {}
        # Lessons written by ReflectionAgent.
        self._memory_lessons: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        # LLM call log for token-budget tracking.
        self._memory_llm_calls: deque[dict[str, Any]] = deque(maxlen=_MEMORY_LIMIT)
        if not db.is_enabled:
            logger.warning(
                "no database configured; the store is keeping the last %d rows per table "
                "in memory only. Nothing is persisted across restarts.",
                _MEMORY_LIMIT,
            )

    @property
    def is_persistent(self) -> bool:
        return self._db.is_enabled

    def session_day(self, at: datetime) -> date:
        """The exchange session ``at`` belongs to. See :func:`_session_day`.

        Exposed as a method so callers that need to bucket timestamps into
        sessions (sizing.py's campaign window) get the same exchange-zone rule
        the store writes with, instead of reimplementing it against UTC.
        """
        return _session_day(at)

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
        ts: datetime | None = None,
    ) -> None:
        """One near-the-money implied-vol reading for ``symbol``.

        Written once per pull, so a symbol accumulates many readings per
        session. IV Rank collapses them to one observation per session day
        (see :meth:`daily_iv_history`) — the cadence here only decides how
        fresh the *current* reading is, never how long the rank's window is.

        ``ts`` dates the reading. It exists for the historical backfill
        (:mod:`options_m.iv_backfill`), which reconstructs past sessions from
        option bars; live callers leave it unset and get now.
        """
        symbol = symbol.upper()
        if not self._db.is_enabled:
            self._memory_iv_history.setdefault(symbol, deque(maxlen=_MEMORY_LIMIT)).appendleft(
                {
                    "ts": ts or _now(),
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
                "INSERT INTO iv_history (symbol, iv_atm, dte, spot, payload, ts) "
                "VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()))",
                (
                    symbol,
                    _as_decimal(iv_atm),
                    dte,
                    _as_decimal(spot),
                    json.dumps(payload) if payload else None,
                    ts,
                ),
            )
            await conn.commit()

    async def append_iv_snapshots(self, symbol: str, readings: list[dict[str, Any]]) -> int:
        """Bulk-insert dated IV readings. Returns the number written.

        The backfill path: one row per past session, each carrying its own
        ``ts``. Rows are inserted as given — the caller is responsible for not
        re-writing a session it already holds (see :meth:`iv_session_days`),
        because the live writer shares this table and a uniqueness constraint
        on the session day would reject its second reading of the minute.
        """
        if not readings:
            return 0
        symbol = symbol.upper()
        if not self._db.is_enabled:
            bucket = self._memory_iv_history.setdefault(symbol, deque(maxlen=_MEMORY_LIMIT))
            # appendleft keeps the deque newest-first, so the oldest reading
            # has to go in first.
            for reading in sorted(readings, key=lambda row: cast(datetime, row["ts"])):
                bucket.appendleft({**reading, "symbol": symbol})
            return len(readings)
        rows = [
            (
                symbol,
                _as_decimal(_maybe_float(reading.get("iv_atm"))),
                reading.get("dte"),
                _as_decimal(_maybe_float(reading.get("spot"))),
                json.dumps(reading["payload"]) if reading.get("payload") else None,
                reading["ts"],
            )
            for reading in readings
        ]
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO iv_history (symbol, iv_atm, dte, spot, payload, ts) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                rows,
            )
            await conn.commit()
        return len(rows)

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
        """This symbol's raw implied-vol readings, newest first.

        Every reading, at whatever cadence they were written — so ``limit``
        counts *rows*, which at a one-minute pulse is minutes, not days. Only
        the dashboard and diagnostics want that. IV Rank and IV percentile go
        through :meth:`daily_iv_history` instead.
        """
        symbol = symbol.upper()
        if not self._db.is_enabled:
            return list(self._memory_iv_history.get(symbol, ()))[:limit]
        return await self._fetch(
            "SELECT ts, symbol, iv_atm, dte, spot "
            "FROM iv_history WHERE symbol = %s ORDER BY ts DESC LIMIT %s",
            (symbol, limit),
        )

    async def daily_iv_history(
        self, symbol: str, *, days: int = IV_RANK_WINDOW_DAYS
    ) -> list[dict[str, Any]]:
        """One ATM-IV observation per exchange session, newest first.

        The last reading of each session day, capped at ``days`` sessions —
        the daily series IV Rank is defined over. Collapsing to one row per day
        is what makes the window a year rather than the last few hours: the
        live writer appends a reading every minute, so 252 rows of
        :meth:`recent_iv` is 252 minutes.

        Rows with no ``iv_atm`` are skipped: an unsolvable IV is an absent
        observation, and letting it through would sink the window's minimum.
        """
        symbol = symbol.upper()
        if not self._db.is_enabled:
            latest_per_day: dict[date, dict[str, Any]] = {}
            # The deque is newest-first, so the first row seen for a day is
            # that day's last reading.
            for row in self._memory_iv_history.get(symbol, ()):
                if _maybe_float(row.get("iv_atm")) is None:
                    continue
                day = _session_day(cast(datetime, row["ts"]))
                if day not in latest_per_day:
                    latest_per_day[day] = {**row, "session_day": day}
            return [latest_per_day[day] for day in sorted(latest_per_day, reverse=True)[:days]]
        # The time bound only keeps the scan off the old end of the index; the
        # LIMIT is what caps the window at `days` sessions.
        cutoff = _now() - timedelta(days=_calendar_span_for(days))
        # ``payload`` is selected so this path returns the same shape as the
        # in-memory one, provenance included: it is how a judge tells a
        # reconstructed observation from a live reading.
        return await self._fetch(
            "SELECT DISTINCT ON (session_day) "
            "       session_day, ts, symbol, iv_atm, dte, spot, payload FROM ("
            "  SELECT (ts AT TIME ZONE 'America/New_York')::date AS session_day,"
            "         ts, symbol, iv_atm, dte, spot, payload"
            "  FROM iv_history"
            "  WHERE symbol = %s AND iv_atm IS NOT NULL AND ts >= %s"
            ") readings ORDER BY session_day DESC, ts DESC LIMIT %s",
            (symbol, cutoff, days),
        )

    async def iv_session_days(self, symbol: str, *, days: int = IV_RANK_WINDOW_DAYS) -> set[date]:
        """Session days this symbol already has an ATM-IV observation for.

        The backfill reads this to know which sessions it still has to
        reconstruct, so a restart does not re-fetch and re-insert a year of
        bars it already holds.
        """
        rows = await self.daily_iv_history(symbol, days=days)
        return {
            cast(date, row["session_day"])
            for row in rows
            if isinstance(row.get("session_day"), date)
        }

    async def iv_rank_and_percentile(
        self,
        symbol: str,
        *,
        days: int = IV_RANK_WINDOW_DAYS,
        min_days: int = IV_RANK_MIN_DAYS,
    ) -> tuple[float | None, float | None, int]:
        """``(iv_rank, iv_percentile, sessions_used)`` over the daily series.

        Both statistics come off one read of the same window, which is also
        what keeps them consistent with each other. Both are ``None`` until the
        window holds ``min_days`` distinct sessions: a rank computed over a
        handful of observations swings tens of points on a tick of IV and
        describes the sample, not the vol regime. ``sessions_used`` is returned
        either way so the caller can publish the sample size alongside the
        numbers.
        """
        rows = await self.daily_iv_history(symbol, days=days)
        # daily_iv_history is newest-first; both statistics want chronological
        # order so the *last* value is the current one.
        values = [_maybe_float(row.get("iv_atm")) for row in reversed(rows)]
        observations = [value for value in values if value is not None]
        if len(observations) < max(min_days, 2):
            return None, None, len(observations)
        return iv_rank(values), iv_percentile(values), len(observations)

    async def iv_rank_for(
        self,
        symbol: str,
        *,
        days: int = IV_RANK_WINDOW_DAYS,
        min_days: int = IV_RANK_MIN_DAYS,
    ) -> float | None:
        """IV Rank of ``symbol``'s latest ATM IV over its last ``days`` sessions.

        ``None`` until the daily series holds ``min_days`` sessions. This is the
        number the volatility analyst reasons about.
        """
        rank, _pctile, _sessions = await self.iv_rank_and_percentile(
            symbol, days=days, min_days=min_days
        )
        return rank

    async def recent_lessons(self, symbol: str | None = None, n: int = 3) -> list[str]:
        """Most recent lessons for a symbol, or portfolio-wide when symbol is None.

        ReflectionAgent writes these; until then returns an empty list so
        evidence.py's shape is stable and callers can wire it now.
        """
        if not self._db.is_enabled:
            rows = list(self._memory_lessons)
            if symbol is not None:
                rows = [r for r in rows if r.get("symbol") == symbol.upper()]
            return [str(r["lesson"]) for r in rows[:n]]
        if symbol is not None:
            rows = await self._fetch(
                "SELECT lesson FROM lessons WHERE symbol = %s ORDER BY ts DESC LIMIT %s",
                (symbol.upper(), n),
            )
        else:
            rows = await self._fetch(
                "SELECT lesson FROM lessons ORDER BY ts DESC LIMIT %s",
                (n,),
            )
        return [str(row["lesson"]) for row in rows]

    async def is_kill_switch_engaged(self) -> bool:
        if not self._db.is_enabled:
            return self._memory_kill_switch[0]
        rows = await self._fetch("SELECT engaged FROM kill_switch WHERE id = 1", ())
        if not rows:
            return False
        return bool(rows[0]["engaged"])

    async def kill_switch_status(self) -> dict[str, Any]:
        """Engaged flag plus *why* and *when*, for the admin surface.

        ``is_kill_switch_engaged`` answers the only question the agents need and
        is deliberately kept to a bool. An operator looking at a halted system
        needs the other two columns as well — a halt with no reason and no
        timestamp is indistinguishable from one nobody remembers engaging.
        """
        if not self._db.is_enabled:
            engaged, reason = self._memory_kill_switch
            return {"engaged": engaged, "reason": reason, "updated_at": None}
        rows = await self._fetch(
            "SELECT engaged, reason, updated_at FROM kill_switch WHERE id = 1", ()
        )
        if not rows:
            return {"engaged": False, "reason": None, "updated_at": None}
        row = rows[0]
        return {
            "engaged": bool(row["engaged"]),
            "reason": row["reason"],
            "updated_at": row["updated_at"],
        }

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
        for the full detail of one. ``is_mock`` reads ``evidence->>'mock'``:
        seed data (see scripts/seed_demo_data.py) sets it so the dashboard can
        label synthetic rows honestly rather than let them read as real agent
        output.
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
                    "is_mock": bool((row["evidence"] or {}).get("mock", False)),
                }
                for row in rows[:limit]
            ]
        if status is not None:
            return await self._fetch(
                "SELECT id, ts, underlying, status, "
                "(arguments IS NOT NULL) AS has_arguments, "
                "(verdict IS NOT NULL) AS has_verdict, "
                "COALESCE((evidence->>'mock')::boolean, false) AS is_mock "
                "FROM proposals WHERE status = %s ORDER BY ts DESC LIMIT %s",
                (status, limit),
            )
        return await self._fetch(
            "SELECT id, ts, underlying, status, "
            "(arguments IS NOT NULL) AS has_arguments, "
            "(verdict IS NOT NULL) AS has_verdict, "
            "COALESCE((evidence->>'mock')::boolean, false) AS is_mock "
            "FROM proposals ORDER BY ts DESC LIMIT %s",
            (limit,),
        )

    async def proposals_since(self, since: datetime) -> list[dict[str, Any]]:
        """``underlying``/``ts``/``status`` of every proposal at or after ``since``.

        Newest first. StrategistAgent's per-symbol cooldown and its per-day
        proposal caps both key on this — they must see proposals of every
        status, not just ``pending``, or a re-proposed name that was rejected
        or dry-run-approved last tick would not be counted.
        """
        if not self._db.is_enabled:
            rows = [
                {"underlying": row["underlying"], "ts": row["ts"], "status": row["status"]}
                for row in self._memory_proposals.values()
                if isinstance(row["ts"], datetime) and row["ts"] >= since
            ]
            rows.sort(key=lambda row: row["ts"], reverse=True)
            return rows
        return await self._fetch(
            "SELECT underlying, ts, status FROM proposals WHERE ts >= %s ORDER BY ts DESC",
            (since,),
        )

    async def conviction_outcomes(self, limit: int = 200) -> list[dict[str, Any]]:
        """``{conviction, pnl_pct, underlying, ts}`` for every closed structure.

        Answers "does this system's stated conviction predict anything?", which
        nothing asked before. The link is already in the data and needs no new
        writer: PositionManagerAgent enriches each open position with the
        ``proposal_id`` that opened it and recomputes ``pnl_pct`` every tick, and
        when StrategistAgent decides to exit it stores that whole payload as the
        close proposal's ``evidence``. So a close proposal carries both the P&L
        the position was closed on and a pointer back to the proposal whose
        intent holds the conviction.

        ``pnl_pct`` is the unrealized figure at the moment the exit was decided,
        not the realized fill — a documented approximation. The fill happens
        seconds later at a price this system does not attribute back to the
        structure, and the difference is a spread, not a signal.

        Newest first. Rows missing either half are skipped rather than defaulted:
        a conviction with no outcome carries no information, and an outcome with
        no conviction cannot be attributed.
        """
        closes = await self._closing_proposals(limit)
        if not closes:
            return []

        opened_ids = {row["opened_by"] for row in closes}
        convictions = await self._convictions_for(opened_ids)

        outcomes: list[dict[str, Any]] = []
        for row in closes:
            conviction = convictions.get(row["opened_by"])
            if conviction is None:
                continue
            outcomes.append(
                {
                    "conviction": conviction,
                    "pnl_pct": row["pnl_pct"],
                    "underlying": row["underlying"],
                    "ts": row["ts"],
                }
            )
        return outcomes

    async def _closing_proposals(self, limit: int) -> list[dict[str, Any]]:
        """Close proposals whose evidence carries both an opener and a P&L."""
        rows: list[dict[str, Any]]
        if not self._db.is_enabled:
            rows = sorted(
                self._memory_proposals.values(),
                key=lambda row: row["ts"],
                reverse=True,
            )
            candidates = [
                {
                    "underlying": row["underlying"],
                    "ts": row["ts"],
                    "evidence": row.get("evidence"),
                    "intent": row.get("intent"),
                }
                for row in rows
            ]
        else:
            candidates = await self._fetch(
                "SELECT underlying, ts, evidence, intent FROM proposals "
                "WHERE intent->>'action' = 'close' ORDER BY ts DESC LIMIT %s",
                (limit,),
            )

        result: list[dict[str, Any]] = []
        for row in candidates:
            intent = row.get("intent")
            if not isinstance(intent, dict) or intent.get("action") != "close":
                continue
            evidence = row.get("evidence")
            if not isinstance(evidence, dict):
                continue
            opened_by = evidence.get("proposal_id")
            pnl_pct = _maybe_float(evidence.get("pnl_pct"))
            if not isinstance(opened_by, int) or pnl_pct is None:
                continue
            result.append(
                {
                    "opened_by": opened_by,
                    "pnl_pct": pnl_pct,
                    "underlying": row["underlying"],
                    "ts": row["ts"],
                }
            )
            if len(result) >= limit:
                break
        return result

    async def _convictions_for(self, proposal_ids: set[int]) -> dict[int, float]:
        """``{proposal_id: conviction}`` for the proposals that opened a trade."""
        if not proposal_ids:
            return {}
        rows: list[dict[str, Any]]
        if not self._db.is_enabled:
            rows = [
                {"id": pid, "intent": self._memory_proposals[pid].get("intent")}
                for pid in proposal_ids
                if pid in self._memory_proposals
            ]
        else:
            rows = await self._fetch(
                "SELECT id, intent FROM proposals WHERE id = ANY(%s)",
                (list(proposal_ids),),
            )
        convictions: dict[int, float] = {}
        for row in rows:
            intent = row.get("intent")
            if not isinstance(intent, dict):
                continue
            conviction = _maybe_float(intent.get("conviction"))
            if conviction is not None:
                convictions[int(row["id"])] = conviction
        return convictions

    async def active_proposal_underlyings(
        self, *, exclude_proposal_id: int | None = None
    ) -> set[str]:
        """Uppercased underlyings that currently hold a position slot.

        A proposal that is ``pending``, ``dry_run_approved`` or ``submitted``
        stands for an intended or resting position ``get_all_positions`` does
        not show yet. Counting these stops the same symbol being re-proposed
        and re-submitted while its first order rests unfilled. ``exclude_proposal_id``
        drops the proposal currently being executed so it does not block itself.
        """
        if not self._db.is_enabled:
            return {
                str(row["underlying"]).upper()
                for row in self._memory_proposals.values()
                if row["status"] in _ACTIVE_PROPOSAL_STATUSES
                and row["id"] != exclude_proposal_id
            }
        if exclude_proposal_id is None:
            rows = await self._fetch(
                "SELECT DISTINCT underlying FROM proposals WHERE status = ANY(%s)",
                (list(_ACTIVE_PROPOSAL_STATUSES),),
            )
        else:
            rows = await self._fetch(
                "SELECT DISTINCT underlying FROM proposals "
                "WHERE status = ANY(%s) AND id <> %s",
                (list(_ACTIVE_PROPOSAL_STATUSES), exclude_proposal_id),
            )
        return {str(row["underlying"]).upper() for row in rows}

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
        """Orders not yet in a settled state — ExecutionAgent's reconcile work list.

        A denylist, not ``status = 'submitted'``: the first reconcile tick
        overwrites the local ``submitted`` with whatever the broker returns
        (``accepted``, ``new``, ``partially_filled`` …), so matching only
        ``submitted`` would drop those rows from reconcile forever and an order
        that was not ``filled`` on its first poll would never have its fill or
        its rejection recorded.
        """
        if not self._db.is_enabled:
            rows = [
                row
                for row in self._memory_orders.values()
                if str(row["status"]).lower() not in _SETTLED_ORDER_STATES
            ]
            rows.sort(key=lambda row: row["submitted_at"])
            return rows[:limit]
        return await self._fetch(
            "SELECT id, proposal_id, client_order_id, submitted_at, status, request, "
            "response, filled_qty, filled_avg_price, error FROM orders "
            "WHERE lower(status) <> ALL(%s) ORDER BY submitted_at ASC LIMIT %s",
            (list(_SETTLED_ORDER_STATES), limit),
        )

    async def working_order_underlyings(self) -> set[str]:
        """Uppercased OCC underlyings of every order still working at the broker.

        A submitted-but-unfilled limit order is not a position, so a portfolio
        snapshot built from ``get_all_positions`` alone misses it. Each leg's
        symbol is parsed from the stored order request; a leg that will not
        parse as OCC is skipped rather than guessed at.
        """
        roots: set[str] = set()
        for order in await self.orders_in_flight():
            request = order.get("request") or {}
            symbols: list[str] = []
            if isinstance(request.get("symbol"), str):
                symbols.append(request["symbol"])
            for leg in request.get("legs") or []:
                if isinstance(leg, dict) and isinstance(leg.get("symbol"), str):
                    symbols.append(leg["symbol"])
            for symbol in symbols:
                occ = parse_occ_symbol(symbol)
                if occ is not None:
                    roots.add(occ.underlying.upper())
        return roots

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
    # Sole writers: EvidenceCollector (live, one reading per pull) and
    # iv_backfill (historical, one per past session). There was a third,
    # save_iv_history, which nothing ever called: it wrote a different column
    # set to the same table -- no dte, no spot -- so a row of its making would
    # have entered the daily series as an observation that could not be
    # attributed to a tenor. Removed rather than left waiting to be wired up.

    # ---- Market calendar cache ------------------------------------------
    # Sole writer: MarketPulseAgent. Every other agent's "is the market open"
    # check goes through market_is_open() below -- never a live get_clock call.

    async def upsert_market_calendar(self, rows: list[dict[str, Any]]) -> None:
        """Upsert calendar rows. Each row: {date, open (datetime), close (datetime),
        session_type}. ``open``/``close`` must already be timezone-aware."""
        if not rows:
            return
        if not self._db.is_enabled:
            for row in rows:
                self._memory_calendar[row["date"]] = row
            return
        payload = [
            (row["date"], row["open"], row["close"], row.get("session_type", "full"))
            for row in rows
        ]
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO market_calendar (date, open, close, session_type) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (date) DO UPDATE SET open = EXCLUDED.open, "
                "close = EXCLUDED.close, session_type = EXCLUDED.session_type",
                payload,
            )
            await conn.commit()

    async def calendar_max_date(self) -> date | None:
        """The furthest-out date currently cached, or None if never populated.

        MarketPulseAgent uses this to decide whether the rolling window needs
        extending -- it does not track a separate "last refreshed" timestamp,
        because the window shrinking under the configured margin is itself the
        signal that a refresh is due.
        """
        if not self._db.is_enabled:
            return max(self._memory_calendar) if self._memory_calendar else None
        rows = await self._fetch("SELECT max(date) AS max_date FROM market_calendar", ())
        value = rows[0]["max_date"] if rows else None
        return cast("date | None", value)

    async def calendar_min_date(self) -> date | None:
        """The earliest date currently cached, or None if never populated.

        The counterpart to :meth:`calendar_max_date`: it tells MarketPulseAgent
        whether the backward half of the window is covered, so a cache written
        by an older build (forward-only, starting at today) heals itself
        instead of staying permanently unable to name the last session.
        """
        if not self._db.is_enabled:
            return min(self._memory_calendar) if self._memory_calendar else None
        rows = await self._fetch("SELECT min(date) AS min_date FROM market_calendar", ())
        value = rows[0]["min_date"] if rows else None
        return cast("date | None", value)

    async def sessions_between(self, start: date, end: date) -> int:
        """Cached trading sessions in ``[start, end]``, both ends inclusive.

        Counted from the calendar rather than from calendar-day arithmetic
        because a two-or-three-session campaign that spans a weekend would
        otherwise report itself as already over. Rows missing from the cache are
        simply not counted — the same conservative direction as
        :meth:`market_is_open`, since undercounting elapsed sessions makes a
        campaign look *younger*, which sizes trades down rather than up.
        """
        if end < start:
            return 0
        if not self._db.is_enabled:
            return sum(1 for day in self._memory_calendar if start <= day <= end)
        rows = await self._fetch(
            "SELECT count(*) AS sessions FROM market_calendar WHERE date BETWEEN %s AND %s",
            (start, end),
        )
        return int(rows[0]["sessions"]) if rows else 0

    async def market_is_open(self, at: datetime) -> bool:
        """Local, cache-only market-open check -- never calls the broker.

        A missing calendar row (weekend, holiday, or a gap in the cache) reads
        as closed. That is the conservative direction: opening a real position
        because of a caching gap would be the dangerous failure, sitting out a
        session because of one is merely a missed opportunity.
        """
        local_date = at.astimezone(_EXCHANGE_TZ).date()
        if not self._db.is_enabled:
            row = self._memory_calendar.get(local_date)
        else:
            rows = await self._fetch(
                "SELECT open, close FROM market_calendar WHERE date = %s", (local_date,)
            )
            row = rows[0] if rows else None
        if row is None:
            return False
        return bool(row["open"] <= at <= row["close"])

    async def last_session_close(self, at: datetime) -> datetime | None:
        """Close time of the most recent session that had opened by ``at``.

        Read only by the REPLAY_LAST_SESSION testing mode (see session.py).
        Selecting on ``open <= at`` rather than ``close <= at`` means an
        in-progress session returns its own close, not yesterday's.

        Returns None when the calendar cache holds no past session, which is
        what keeps the replay mode honest: it can replay a real session or
        nothing at all, never a fabricated one.
        """
        if not self._db.is_enabled:
            closes = [row["close"] for row in self._memory_calendar.values() if row["open"] <= at]
            return max(closes) if closes else None
        rows = await self._fetch(
            "SELECT close FROM market_calendar WHERE open <= %s ORDER BY open DESC LIMIT 1",
            (at,),
        )
        if not rows:
            return None
        close = rows[0]["close"]
        return close if isinstance(close, datetime) else None

    # ---- Account cache --------------------------------------------------
    # Sole writer: MarketPulseAgent, piggybacked on the get_account_info /
    # get_account_config call it already makes every tick for equity_curve.

    async def upsert_account(
        self,
        *,
        equity: float | None,
        cash: float | None,
        buying_power: float | None,
        options_trading_level: int | None,
    ) -> None:
        if not self._db.is_enabled:
            self._memory_account = {
                "equity": equity,
                "cash": cash,
                "buying_power": buying_power,
                "options_trading_level": options_trading_level,
                "updated_at": _now(),
            }
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO account (id, equity, cash, buying_power, "
                "options_trading_level, updated_at) VALUES (1, %s, %s, %s, %s, now()) "
                "ON CONFLICT (id) DO UPDATE SET equity = EXCLUDED.equity, "
                "cash = EXCLUDED.cash, buying_power = EXCLUDED.buying_power, "
                "options_trading_level = EXCLUDED.options_trading_level, updated_at = now()",
                (
                    _as_decimal(equity),
                    _as_decimal(cash),
                    _as_decimal(buying_power),
                    options_trading_level,
                ),
            )
            await conn.commit()

    async def get_cached_account(self) -> dict[str, Any] | None:
        """The last account snapshot MarketPulseAgent wrote. Never a live call."""
        if not self._db.is_enabled:
            return self._memory_account
        rows = await self._fetch(
            "SELECT equity, cash, buying_power, options_trading_level, updated_at "
            "FROM account WHERE id = 1",
            (),
        )
        return rows[0] if rows else None

    # ---- Positions cache ------------------------------------------------
    # Sole writer: PositionManagerAgent, piggybacked on its existing per-tick
    # get_all_positions call. Keyed by underlying symbol, current state only --
    # this table is overwritten in place, unlike the append-only tables above.

    async def upsert_position(self, symbol: str, payload: dict[str, Any]) -> None:
        if not self._db.is_enabled:
            self._memory_positions[symbol] = payload
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO positions (symbol, payload, updated_at) "
                "VALUES (%s, %s, now()) "
                "ON CONFLICT (symbol) DO UPDATE SET payload = EXCLUDED.payload, "
                "updated_at = now()",
                (symbol, json.dumps(payload)),
            )
            await conn.commit()

    async def remove_position(self, symbol: str) -> None:
        if not self._db.is_enabled:
            self._memory_positions.pop(symbol, None)
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM positions WHERE symbol = %s", (symbol,))
            await conn.commit()

    async def get_cached_positions(self) -> list[dict[str, Any]]:
        """Every currently-open position, from the local cache -- never a live call.

        Each row is ``{"symbol": ..., "payload": ..., "updated_at": ...}`` in both
        the Postgres and in-memory paths -- the shape must match so callers never
        need an `is_persistent` branch of their own.
        """
        if not self._db.is_enabled:
            return [
                {"symbol": symbol, "payload": payload, "updated_at": None}
                for symbol, payload in sorted(self._memory_positions.items())
            ]
        rows = await self._fetch(
            "SELECT symbol, payload, updated_at FROM positions ORDER BY symbol", ()
        )
        return rows

    async def replace_positions(self, payload_by_symbol: dict[str, dict[str, Any]]) -> None:
        """Upsert every currently-open position and drop whatever closed since
        the last tick, in one call -- what PositionManagerAgent calls each
        iteration with the fresh get_all_positions response."""
        existing = {row["symbol"] for row in await self.get_cached_positions()}
        for symbol, payload in payload_by_symbol.items():
            await self.upsert_position(symbol, payload)
        for stale_symbol in existing - set(payload_by_symbol):
            await self.remove_position(stale_symbol)

    # ---- Evidence cache -------------------------------------------------
    # Sole writer: MarketPulseAgent (every 60s, per universe symbol, overwritten
    # in place). StrategistAgent reads from here instead of calling
    # evidence.collect() itself.

    async def upsert_evidence_cache(self, symbol: str, payload: dict[str, Any]) -> None:
        symbol = symbol.upper()
        if not self._db.is_enabled:
            self._memory_evidence[symbol] = {
                "symbol": symbol,
                "payload": payload,
                "updated_at": _now(),
            }
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO evidence (symbol, payload, updated_at) "
                "VALUES (%s, %s, now()) "
                "ON CONFLICT (symbol) DO UPDATE SET payload = EXCLUDED.payload, "
                "updated_at = now()",
                (symbol, json.dumps(payload)),
            )
            await conn.commit()

    async def get_cached_evidence(self, symbol: str) -> dict[str, Any] | None:
        """The latest evidence pack MarketPulseAgent wrote for this symbol.

        Returns a row with keys ``symbol``, ``payload``, ``updated_at``, or
        ``None`` if the symbol has never been collected. Never a live call.
        """
        symbol = symbol.upper()
        if not self._db.is_enabled:
            return self._memory_evidence.get(symbol)
        rows = await self._fetch(
            "SELECT symbol, payload, updated_at FROM evidence WHERE symbol = %s",
            (symbol,),
        )
        return rows[0] if rows else None

    # ---- Lessons --------------------------------------------------------

    async def save_lesson(
        self,
        *,
        symbol: str | None,
        lesson: str,
        source: str,
        reflected_on: str,
    ) -> None:
        """Write one post-trade lesson. ``reflected_on`` is the idempotency key
        (e.g. ``order:42`` or ``proposal:17``) so an item is reflected on at
        most once even if ReflectionAgent runs more than once."""
        row = {
            "ts": _now(),
            "symbol": symbol.upper() if symbol else None,
            "lesson": lesson,
            "source": source,
            "reflected_on": reflected_on,
        }
        if not self._db.is_enabled:
            if not any(r["reflected_on"] == reflected_on for r in self._memory_lessons):
                self._memory_lessons.appendleft(row)
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO lessons (symbol, lesson, source, reflected_on) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (reflected_on) DO NOTHING",
                (row["symbol"], lesson, source, reflected_on),
            )
            await conn.commit()

    # ---- Proposals ------------------------------------------------------

    async def save_proposal(
        self,
        *,
        underlying: str,
        intent: dict[str, Any],
        evidence: dict[str, Any],
        status: str = "pending",
        llm_read: dict[str, Any] | None = None,
        matrix: dict[str, Any] | None = None,
    ) -> int:
        """Insert a new proposal. Returns its id."""
        if not self._db.is_enabled:
            self._memory_proposal_seq += 1
            proposal_id = self._memory_proposal_seq
            self._memory_proposals[proposal_id] = {
                "id": proposal_id,
                "ts": _now(),
                "underlying": underlying,
                "status": status,
                "intent": intent,
                "evidence": evidence,
                "arguments": llm_read,
                "verdict": None,
                "plan": None,
                "error": None,
                "llm_read": llm_read,
                "matrix_verdict": matrix,
            }
            return proposal_id
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO proposals (underlying, intent, evidence, status, "
                "llm_read, matrix_verdict) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    underlying,
                    json.dumps(intent),
                    json.dumps(evidence),
                    status,
                    json.dumps(llm_read) if llm_read is not None else None,
                    json.dumps(matrix) if matrix is not None else None,
                ),
            )
            row = cast("tuple[Any, ...] | None", await cur.fetchone())
            await conn.commit()
            assert row is not None
            return int(row[0])

    # ---- LLM call log ---------------------------------------------------

    async def record_llm_call(
        self,
        *,
        agent: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        ok: bool,
        error: str | None = None,
    ) -> None:
        row = {
            "ts": _now(),
            "agent": agent,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": latency_ms,
            "ok": ok,
            "error": error,
        }
        if not self._db.is_enabled:
            self._memory_llm_calls.appendleft(row)
            return
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO llm_calls (agent, model, prompt_tokens, completion_tokens, "
                "latency_ms, ok, error) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (agent, model, prompt_tokens, completion_tokens, latency_ms, ok, error),
            )
            await conn.commit()

    # ---- Top-candidates helper ------------------------------------------

    async def top_candidates(
        self,
        limit: int = 5,
        max_age_seconds: float = 300.0,
    ) -> list[dict[str, Any]]:
        """Most recent unique-symbol candidates scored by the last batch,
        filtered to those written within max_age_seconds, sorted by score
        descending. Used by StrategistAgent to pick its work symbol.
        """
        all_recent = await self.recent_candidates(limit=limit * 4)
        cutoff = _now().timestamp() - max_age_seconds
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for row in all_recent:
            ts = row.get("ts")
            if ts is not None:
                ts_epoch = ts.timestamp() if hasattr(ts, "timestamp") else 0.0
                if ts_epoch < cutoff:
                    break
            symbol = str(row.get("symbol", "")).upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                result.append(row)
        result.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
        return result[:limit]

    async def get_all_orders(self) -> list[dict[str, Any]]:
        """All orders in the store, for position-to-proposal matching.

        Returns the same shape as :meth:`recent_orders` but without a limit so
        PositionManagerAgent can match any position regardless of age.
        """
        if not self._db.is_enabled:
            return sorted(
                self._memory_orders.values(),
                key=lambda row: row["submitted_at"],
                reverse=True,
            )
        return await self._fetch(
            "SELECT id, proposal_id, client_order_id, submitted_at, status, request, "
            "response, filled_qty, filled_avg_price, error FROM orders "
            "ORDER BY submitted_at DESC",
            (),
        )

    async def _fetch(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        async with self._db.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            columns = [column.name for column in cur.description or []]
            rows = cast("list[tuple[Any, ...]]", await cur.fetchall())
            return [dict(zip(columns, row, strict=True)) for row in rows]
