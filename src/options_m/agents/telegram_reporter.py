"""TelegramReporterAgent -- the periodic portfolio snapshot.

Reads only: the ``positions`` and ``account`` caches ``PositionManagerAgent``
and ``MarketPulseAgent`` already maintain. It never calls the broker, so its
cadence costs nothing at Alpaca and can be tuned freely.

Gated on market hours like every other agent, with one deliberate addition: on
the first tick after the session closes it sends a final snapshot, so the day
ends with a closing figure instead of whatever the last mid-session tick
happened to say.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from options_m import session
from options_m.config import Settings
from options_m.notify import Notifier, format_summary
from options_m.store import Store

logger = logging.getLogger(__name__)


class TelegramReporterAgent:
    """Pushes a portfolio snapshot to Telegram on a fixed cadence."""

    def __init__(self, settings: Settings, store: Store, notifier: Notifier) -> None:
        self._settings = settings
        self._store = store
        self._notifier = notifier
        # Whether the previous tick saw an open market. Starts None so a
        # process that boots after the close does not immediately fire a
        # "session closed" summary for a session it never observed.
        self._was_open: bool | None = None

    @property
    def name(self) -> str:
        return "telegram_reporter"

    @property
    def interval_seconds(self) -> float:
        return self._settings.telegram_summary_interval_seconds

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
        state = await session.current(self._store, self._settings, datetime.now(UTC))
        was_open, self._was_open = self._was_open, state.is_open

        if not state.is_open:
            if not was_open:
                return {"skipped": "market_closed"}
            title = "Session close snapshot"
        else:
            title = "Portfolio snapshot"

        positions = await self._store.get_cached_positions()
        account = await self._store.get_cached_account()
        self._notifier.notify(
            format_summary(
                positions=positions,
                account=account,
                dry_run=self._settings.dry_run,
                title=title,
            )
        )
        detail = {"positions": len(positions), "title": title}
        logger.info("telegram summary sent", extra=detail)
        return detail
