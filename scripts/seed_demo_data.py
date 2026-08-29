"""One-off seed script for local demo/testing.

Inserts a handful of realistic-looking proposals, risk events, and orders
directly through the Store, so the dashboard has something to show before
Phase 3 (the LLM crew) exists to produce real ones. Requires DATABASE_URL —
run against the same Postgres the running backend uses, since the in-memory
fallback is per-process and would not be visible to it.

Usage: python scripts/seed_demo_data.py
"""

from __future__ import annotations

import asyncio

from options_m.config import Settings
from options_m.db import Database
from options_m.store import Store

PROPOSALS: list[dict[str, object]] = [
    {
        "underlying": "SPY",
        "status": "approved",
        "intent": {
            "action": "open",
            "strategy": "debit_call_spread",
            "direction": "bullish",
            "target_delta": 0.35,
            "dte_window": [21, 35],
            "conviction": 0.7,
            "thesis": "SPY holding above the 20-day EMA with rising volume; IV rank is "
            "low enough that a debit spread is cheap relative to recent realized vol.",
            "invalidation": "Daily close below 565.00",
        },
        "evidence": {
            "underlying_price": 571.42,
            "iv_rank": 18.3,
            "put_call_skew": -0.04,
            "news_highlights": ["Fed holds rates steady", "PCE inflation in line with estimates"],
        },
    },
    {
        "underlying": "QQQ",
        "status": "rejected",
        "intent": {
            "action": "open",
            "strategy": "long_call",
            "direction": "bullish",
            "target_delta": 0.5,
            "dte_window": [14, 21],
            "conviction": 0.55,
            "thesis": "Semiconductor strength broadening into megacap tech.",
            "invalidation": "Daily close below 480.00",
        },
        "evidence": {
            "underlying_price": 492.10,
            "iv_rank": 41.2,
            "put_call_skew": 0.11,
        },
        "risk_rule": "max_spread_pct",
        "risk_detail": {
            "symbol": "QQQ250321C00500000",
            "bid": 4.10,
            "ask": 4.85,
            "spread_pct": 0.168,
            "limit": 0.10,
        },
    },
    {
        "underlying": "AAPL",
        "status": "approved",
        "intent": {
            "action": "open",
            "strategy": "cash_secured_put",
            "direction": "neutral_bullish",
            "target_delta": -0.25,
            "dte_window": [30, 45],
            "conviction": 0.6,
            "thesis": "Willing to acquire AAPL at a discount to spot; premium collection "
            "in a low-IV regime.",
            "invalidation": "Daily close below 210.00 on high volume",
        },
        "evidence": {
            "underlying_price": 228.55,
            "iv_rank": 22.7,
        },
    },
    {
        "underlying": "TSLA",
        "status": "rejected",
        "intent": {
            "action": "open",
            "strategy": "long_put",
            "direction": "bearish",
            "target_delta": -0.4,
            "dte_window": [10, 18],
            "conviction": 0.5,
            "thesis": "Deliveries miss widely expected; fading the post-earnings pop.",
            "invalidation": "Daily close above 265.00",
        },
        "evidence": {
            "underlying_price": 248.90,
            "iv_rank": 63.5,
        },
        "risk_rule": "max_positions_per_underlying",
        "risk_detail": {"underlying": "TSLA", "existing_positions": 1, "limit": 1},
    },
    {
        "underlying": "NVDA",
        "status": "pending",
        "intent": {
            "action": "open",
            "strategy": "debit_put_spread",
            "direction": "bearish",
            "target_delta": -0.3,
            "dte_window": [21, 30],
            "conviction": 0.45,
            "thesis": "Hedging elevated single-name concentration ahead of earnings.",
            "invalidation": "Daily close above 145.00",
        },
        "evidence": {
            "underlying_price": 132.18,
            "iv_rank": 55.9,
        },
    },
]


async def main() -> None:
    settings = Settings()
    if not settings.database_url:
        msg = "DATABASE_URL is not set — point it at the same Postgres the backend uses."
        raise SystemExit(msg)

    async with Database(settings) as db:
        store = Store(db)
        for row in PROPOSALS:
            proposal_id = await store.save_proposal(
                underlying=str(row["underlying"]),
                intent=row["intent"],  # type: ignore[arg-type]
                evidence=row["evidence"],  # type: ignore[arg-type]
            )
            status = str(row["status"])
            await store.update_proposal_status(proposal_id, status)

            if status == "rejected":
                await store.record_risk_event(
                    proposal_id=proposal_id,
                    rule=str(row["risk_rule"]),
                    detail=row["risk_detail"],  # type: ignore[arg-type]
                )
            elif status == "approved":
                client_order_id = f"om-demo-{proposal_id}"
                await store.record_order(
                    proposal_id=proposal_id,
                    client_order_id=client_order_id,
                    status="filled",
                    request={"symbol": row["underlying"], "qty": "1", "type": "limit"},
                    response={"id": client_order_id, "status": "filled"},
                )

        # A couple of standalone risk events with no proposal, mirroring a
        # kill-switch halt or a daily-loss breaker firing on its own.
        await store.record_risk_event(
            proposal_id=None,
            rule="daily_loss_halt",
            detail={"daily_pnl_pct": -0.034, "limit_pct": -0.03},
        )
        await store.record_risk_event(
            proposal_id=None,
            rule="min_open_interest",
            detail={"symbol": "AMD250321C00160000", "open_interest": 42, "limit": 100},
        )

        print(f"Seeded {len(PROPOSALS)} proposals and their risk events/orders.")


if __name__ == "__main__":
    asyncio.run(main())
