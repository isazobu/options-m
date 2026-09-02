"""Print one line per profile from the database.

Read-only. Needs DATABASE_URL pointed at the same Postgres the service uses
(the in-memory fallback is per-process and not visible here).

Columns:
  last seen   most recent agent_runs row for the profile
  ticks       agent_runs rows — 0 means the stack never started: the service
              was not restarted after a PROFILES change, or that profile's
              Alpaca keys failed (profiles start in list order and one bad
              broker connection aborts startup, so later ones never run)
  props       proposals ("decisions") recorded — 0 off-hours, since the
              strategist short-circuits while the market is closed
  fills       filled orders — always 0 while DRY_RUN=true (no order is sent)
  open        open positions the broker reports (may pre-date the bot)
  unreal      summed unrealized_pl across those positions
  closed      summed pnl_pct over close-proposals, n = how many

Usage: python scripts/summary.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any

from options_m.config import Settings
from options_m.db import Database
from options_m.runtime import load_profiles


async def _one(cur: Any, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
    await cur.execute(sql, params)
    return await cur.fetchone()  # type: ignore[no-any-return]


async def _names(cur: Any) -> list[str]:
    """Every configured profile (in PROFILES order), then any other ids present."""
    ordered = [profile.name for profile in load_profiles(Settings())]
    await cur.execute(
        "SELECT DISTINCT account_id FROM ("
        "  SELECT account_id FROM agent_runs"
        "  UNION SELECT account_id FROM equity_curve"
        "  UNION SELECT account_id FROM orders"
        "  UNION SELECT account_id FROM proposals"
        ") s"
    )
    seen = {row[0] for row in await cur.fetchall()}
    tail = sorted(seen - set(ordered))
    return [*ordered, *tail]


def _pct(first: float | None, last: float | None) -> str:
    if first is None or last is None or first == 0:
        return "   n/a"
    return f"{(last / first - 1) * 100:+6.2f}%"


def _money(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):,.0f}"


def _when(value: Any) -> str:
    return value.strftime("%m-%d %H:%M") if isinstance(value, datetime) else "never"


async def main() -> None:
    settings = Settings()
    if not settings.database_url:
        msg = "DATABASE_URL is not set — point it at the same Postgres the service uses."
        raise SystemExit(msg)

    async with Database(settings) as db, db.connection() as conn, conn.cursor() as cur:
        names = await _names(cur)
        if not names:
            print("no profiles configured and no rows found")
            return

        header = (
            f"{'name':<20} {'last seen':<12} {'ticks':>6} {'props':>6} {'fills':>6} "
            f"{'open':>5} {'equity (first -> last)':<24} {'change':>8} "
            f"{'unreal':>10} {'closed':>16}"
        )
        print(header)
        print("-" * len(header))

        for name in names:
            runs_row = await _one(
                cur,
                "SELECT count(*), max(started_at) FROM agent_runs WHERE account_id = %s",
                (name,),
            )
            props_row = await _one(
                cur,
                "SELECT count(*) FROM proposals WHERE account_id = %s",
                (name,),
            )
            first_row = await _one(
                cur,
                "SELECT equity FROM equity_curve WHERE account_id = %s "
                "AND equity IS NOT NULL ORDER BY ts ASC LIMIT 1",
                (name,),
            )
            last_row = await _one(
                cur,
                "SELECT equity FROM equity_curve WHERE account_id = %s "
                "AND equity IS NOT NULL ORDER BY ts DESC LIMIT 1",
                (name,),
            )
            fills_row = await _one(
                cur,
                "SELECT count(*) FROM orders WHERE account_id = %s AND lower(status) = 'filled'",
                (name,),
            )
            pos_row = await _one(
                cur,
                "SELECT count(*), "
                "COALESCE(SUM(NULLIF(payload->>'unrealized_pl', '')::numeric), 0) "
                "FROM positions WHERE account_id = %s",
                (name,),
            )
            closed_row = await _one(
                cur,
                "SELECT COALESCE(SUM(NULLIF(evidence->>'pnl_pct', '')::numeric), 0), count(*) "
                "FROM proposals WHERE account_id = %s AND intent->>'action' = 'close' "
                "AND evidence ? 'pnl_pct'",
                (name,),
            )

            ticks = runs_row[0] if runs_row else 0
            last_seen = _when(runs_row[1] if runs_row else None)
            props = props_row[0] if props_row else 0
            first = float(first_row[0]) if first_row and first_row[0] is not None else None
            last = float(last_row[0]) if last_row and last_row[0] is not None else None
            fills = fills_row[0] if fills_row else 0
            open_count = pos_row[0] if pos_row else 0
            unreal = pos_row[1] if pos_row else 0
            closed_sum = closed_row[0] if closed_row else 0
            closed_n = closed_row[1] if closed_row else 0

            curve = f"{_money(first)} -> {_money(last)}"
            print(
                f"{name:<20} {last_seen:<12} {ticks:>6} {props:>6} {fills:>6} "
                f"{open_count:>5} {curve:<24} {_pct(first, last):>8} "
                f"{_money(unreal):>10} {float(closed_sum):>+9.3f} (n={closed_n})"
            )

    if settings.dry_run:
        print("\nDRY_RUN is on: no orders are placed, so 'fills' stays 0 for every profile.")
    if settings.profiles is None and os.environ.get("PROFILES") is None:
        print("PROFILES is unset: only the 'default' profile exists.")


if __name__ == "__main__":
    asyncio.run(main())
