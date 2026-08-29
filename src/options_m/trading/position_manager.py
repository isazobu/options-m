"""PositionManagerAgent -- owns the local positions cache.

Runs every minute. Its first and, for now, only job is the one described in
docs/plan/00-MASTER.md's local-cache design: it is the **sole writer** of the
``positions`` table, piggybacked on the same ``get_all_positions`` call it
needs anyway. ``MarketPulseAgent`` (positions_count for the equity curve),
``StrategistAgent`` (Phase 3's "already positioned in this underlying"
pre-filter) and ``ExecutionAgent`` (Phase 2's per-underlying cap) all read
this cache instead of calling ``get_all_positions`` / ``get_open_position``
themselves.

Deterministic exit rules (profit target, stop loss, DTE exit, thesis
invalidation, closing via ``close_position`` / a multi-leg closing order) are
specified in docs/plan/phase-4-position-reflection-dashboard.md and land once
Phase 2's ``orders`` table and ``OrderPlan`` model exist to match a position
back to the proposal that opened it -- building that logic against data that
does not exist yet would mean guessing at a shape Phase 2 has not committed
to. This module is intentionally scoped to the cache-ownership half of the
job until then.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from options_m.config import Settings
from options_m.mcp_client import AlpacaMcp
from options_m.store import Store

logger = logging.getLogger(__name__)

# Standard OCC option symbol: 1-6 letter root, YYMMDD expiry, C/P, 8-digit
# strike (thousandths of a dollar). Anything that does not match this is
# treated as its own underlying rather than guessed at -- a stock position
# (which this system never intentionally holds) falls through this path
# safely instead of raising.
_OCC_SYMBOL_RE = re.compile(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$")


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
        positions = await self._mcp.get_all_positions()

        grouped: dict[str, list[dict[str, Any]]] = {}
        for position in positions:
            underlying = _underlying_symbol(position)
            grouped.setdefault(underlying, []).append(position)

        # {underlying: {"legs": [...]}} -- a list, not a single position dict,
        # because one options structure opens as multiple legs (up to 4) that
        # each show up as their own get_all_positions entry sharing an
        # underlying. Grouping here is what lets Phase 2/3's "max one open
        # structure per underlying" checks read a single row per symbol.
        payload_by_symbol = {
            underlying: {"legs": legs} for underlying, legs in grouped.items()
        }
        await self._store.replace_positions(payload_by_symbol)

        detail: dict[str, Any] = {
            "open_underlyings": len(grouped),
            "open_legs": len(positions),
        }
        logger.info("position pulse", extra=detail)
        return detail


def _underlying_symbol(position: dict[str, Any]) -> str:
    """The underlying ticker for one get_all_positions entry.

    Parses the OCC option symbol rather than trusting an ``underlying_symbol``
    field that may or may not be present across API versions -- the regex is
    the one documented, stable part of the contract. Falls through to the raw
    symbol for anything that is not a recognisable OCC symbol (a stock
    position, which this system does not intentionally hold, or an
    unfamiliar shape) rather than raising: grouping by the wrong key here
    costs the per-underlying cap a clean read, not correctness of an order.
    """
    symbol = str(position.get("symbol", "")).upper()
    match = _OCC_SYMBOL_RE.match(symbol)
    if match:
        return match.group(1)
    return symbol
