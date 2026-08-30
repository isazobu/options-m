"""PositionManagerAgent -- sole writer of the local positions cache.

Runs every minute. Owns the ``positions`` table and nothing else: it fetches
``get_all_positions``, groups legs by underlying, marks to market, enriches
each row with the originating proposal metadata, and persists. Exit decisions
belong to ``StrategistAgent``, which reads this cache each iteration and writes
a ``close`` proposal when a threshold is crossed. ``ExecutionAgent`` then acts
on that proposal exactly as it does for open proposals.

The ``pnl_pct`` field added to each payload avoids StrategistAgent having to
re-derive it from raw market_value / unrealized_pl every tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from options_m.config import Settings
from options_m.mcp_client import AlpacaMcp
from options_m.store import Store

logger = logging.getLogger(__name__)

_OCC_SYMBOL_RE = re.compile(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$")

_ENRICH_KEYS = ("proposal_id", "entry_price", "opened_at", "strategy")


class PositionManagerAgent:
    """Sole writer of the local ``positions`` cache."""

    def __init__(self, settings: Settings, mcp: AlpacaMcp, store: Store) -> None:
        self._settings = settings
        self._mcp = mcp
        self._store = store

    @property
    def name(self) -> str:
        return "position_manager"

    @property
    def interval_seconds(self) -> float:
        return self._settings.position_manager_interval_seconds

    async def step(self) -> None:
        """One iteration. Raises on failure so the supervisor can back off."""
        started = time.monotonic()
        ok = True
        error: str | None = None
        detail: dict[str, Any] = {}
        try:
            detail = await self._run()
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await self._store.record_agent_run(
                self.name,
                duration_ms=int((time.monotonic() - started) * 1000),
                ok=ok,
                error=error,
                detail=detail or None,
            )

    async def _run(self) -> dict[str, Any]:
        positions, existing_rows, all_orders = await asyncio.gather(
            self._mcp.get_all_positions(),
            self._store.get_cached_positions(),
            self._store.get_all_orders(),
        )

        existing_by_symbol: dict[str, dict[str, Any]] = {
            row["symbol"]: row["payload"] for row in existing_rows
        }

        grouped: dict[str, list[dict[str, Any]]] = {}
        for position in positions:
            underlying = _underlying_symbol(position)
            grouped.setdefault(underlying, []).append(position)

        payload_by_symbol: dict[str, dict[str, Any]] = {}
        for underlying, legs in grouped.items():
            old_payload = existing_by_symbol.get(underlying, {})

            payload: dict[str, Any] = {"legs": legs}
            payload["unrealized_pl"] = _total_unrealized_pl(legs)
            payload["market_value"] = _total_market_value(legs)

            # Carry forward enrichment that was resolved in a previous tick.
            for key in _ENRICH_KEYS:
                if key in old_payload:
                    payload[key] = old_payload[key]

            # Enrich on first appearance: link to the originating proposal.
            if "proposal_id" not in payload:
                _enrich_from_orders(payload, legs, all_orders)

            # Pre-compute pnl_pct so StrategistAgent can read it directly.
            payload["pnl_pct"] = _compute_pnl_pct(payload)

            payload_by_symbol[underlying] = payload

        total_unrealized_pl = _total_unrealized_pl(positions)
        total_market_value = _total_market_value(positions)

        await self._store.replace_positions(payload_by_symbol)

        detail: dict[str, Any] = {
            "open_underlyings": len(grouped),
            "open_legs": len(positions),
            "unrealized_pl": round(total_unrealized_pl, 2),
            "market_value": round(total_market_value, 2),
        }
        logger.info("position pulse", extra=detail)
        return detail


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _enrich_from_orders(
    payload: dict[str, Any],
    legs: list[dict[str, Any]],
    all_orders: list[dict[str, Any]],
) -> None:
    """Fill proposal_id, entry_price, opened_at, strategy into payload in-place.

    Scans the orders cache for a filled entry order (``om-`` prefix) whose
    request legs share an OCC symbol with this position's legs. Skips close
    orders (``omc-`` prefix) to avoid matching a position to its own close.
    """
    position_symbols = {str(leg.get("symbol", "")).upper() for leg in legs}
    position_symbols.discard("")

    for order in all_orders:
        if str(order.get("status", "")).lower() not in ("filled", "partially_filled"):
            continue
        client_order_id = str(order.get("client_order_id", ""))
        if client_order_id.startswith("omc-"):
            continue

        request = order.get("request") or {}
        if isinstance(request, str):
            import json

            with contextlib.suppress(Exception):
                request = json.loads(request)

        order_symbols: set[str] = set()
        if isinstance(request, dict):
            for ol in request.get("legs") or []:
                if isinstance(ol, dict):
                    s = str(ol.get("symbol", "")).upper()
                    if s:
                        order_symbols.add(s)
            single = str(request.get("symbol", "")).upper()
            if single:
                order_symbols.add(single)

        if not order_symbols.isdisjoint(position_symbols):
            proposal_id: int | None = None
            if client_order_id.startswith("om-"):
                with contextlib.suppress(ValueError):
                    proposal_id = int(client_order_id[3:])

            payload["proposal_id"] = proposal_id
            payload["entry_price"] = _maybe_float(order.get("filled_avg_price"))
            submitted = order.get("submitted_at")
            payload["opened_at"] = (
                submitted.isoformat()  # type: ignore[union-attr]
                if hasattr(submitted, "isoformat")
                else str(submitted)
            )
            payload["strategy"] = ""
            return


def _compute_pnl_pct(payload: dict[str, Any]) -> float | None:
    """Unrealized P&L as a fraction of entry value. None if uncomputable."""
    unrealized_pl = payload.get("unrealized_pl")
    market_value = payload.get("market_value")
    if unrealized_pl is None or market_value is None:
        return None
    with contextlib.suppress(TypeError, ValueError, ZeroDivisionError):
        unreal = float(unrealized_pl)
        entry_value = float(market_value) - unreal
        if abs(entry_value) > 0.001:
            return unreal / abs(entry_value)
    return None


def _total_unrealized_pl(positions: list[dict[str, Any]]) -> float:
    total = 0.0
    for p in positions:
        with contextlib.suppress(TypeError, ValueError):
            total += float(p.get("unrealized_pl"))  # type: ignore[arg-type]
    return total


def _total_market_value(positions: list[dict[str, Any]]) -> float:
    total = 0.0
    for p in positions:
        with contextlib.suppress(TypeError, ValueError):
            total += abs(float(p.get("market_value")))  # type: ignore[arg-type]
    return total


def _underlying_symbol(position: dict[str, Any]) -> str:
    symbol = str(position.get("symbol", "")).upper()
    match = _OCC_SYMBOL_RE.match(symbol)
    if match:
        return match.group(1)
    return symbol


def _maybe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _now_utc() -> datetime:
    return datetime.now(UTC)
