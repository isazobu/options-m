"""Print a one-line account summary from the database.

Read-only. Needs DATABASE_URL pointed at the same Postgres the service uses
(the in-memory fallback is per-process and not visible here).

Usage: python scripts/summary.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from options_m.config import Settings
from options_m.db import Database


async def _one(cur: Any, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
    await cur.execute(sql, params)
    return await cur.fetchone()  # type: ignore[no-any-return]


async def _names(cur: Any) -> list[str]:
    await cur.execute(
        "SELECT DISTINCT account_id FROM ("
        "  SELECT account_id FROM equity_curve"
        "  UNION SELECT account_id FROM orders"
        "  UNION SELECT account_id FROM proposals"
        ") s"
    )
    return sorted(row[0] for row in await cur.fetchall())


def _pct(first: float | None, last: float | None) -> str:
    if first is None or last is None or first == 0:
        return "   n/a"
    return f"{(last / first - 1) * 100:+6.2f}%"


def _money(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):,.0f}"


async def main() -> None:
    settings = Settings()
    if not settings.database_url:
        msg = "DATABASE_URL is not set — point it at the same Postgres the service uses."
        raise SystemExit(msg)

    async with Database(settings) as db, db.connection() as conn, conn.cursor() as cur:
        names = await _names(cur)
        if not names:
            print("no rows found")
            return

        header = (
            f"{'name':<16} {'equity (first -> last)':<26} {'change':>8} "
            f"{'fills':>6} {'open':>5} {'unreal':>12} {'closed':>16}"
        )
        print(header)
        print("-" * len(header))

        for name in names:
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

            first = float(first_row[0]) if first_row and first_row[0] is not None else None
            last = float(last_row[0]) if last_row and last_row[0] is not None else None
            fills = fills_row[0] if fills_row else 0
            open_count = pos_row[0] if pos_row else 0
            unreal = pos_row[1] if pos_row else 0
            closed_sum = closed_row[0] if closed_row else 0
            closed_n = closed_row[1] if closed_row else 0

            curve = f"{_money(first)} -> {_money(last)}"
            print(
                f"{name:<16} {curve:<26} {_pct(first, last):>8} "
                f"{fills:>6} {open_count:>5} {_money(unreal):>12} "
                f"{float(closed_sum):>+11.3f} (n={closed_n})"
            )


if __name__ == "__main__":
    asyncio.run(main())
