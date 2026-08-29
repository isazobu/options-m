"""The market-session gate every agent shares.

Normally this is a pure local-cache read — ``Store.market_is_open`` against
the ``market_calendar`` rows ``MarketPulseAgent`` maintains, never a live
``get_clock`` call. Three agents ask the same question and must get the same
answer, so they all come through here rather than each reading the cache
their own way.

``REPLAY_LAST_SESSION`` is the single deviation, and it exists for one
purpose: exercising the full autonomous chain out of hours, when a genuinely
closed market short-circuits every agent at its first check and nothing
downstream ever runs. Two properties keep it honest:

- **It cannot invent a session.** It replays the most recent real one from the
  calendar cache; with an empty cache it stays closed.
- **It is interlocked with dry run.** The evidence collected under a replayed
  session is last session's data by construction, so an order built from it
  must never reach the broker. ``__main__.main()`` refuses to start when
  replay is on and ``dry_run`` is off.

Every caller reports :attr:`SessionState.replayed` in its ``agent_runs``
telemetry, so a replayed decision is never indistinguishable from a real one
after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from options_m.config import Settings
from options_m.store import Store


@dataclass(frozen=True, slots=True)
class SessionState:
    """Whether agents should act, and whether that answer was replayed."""

    is_open: bool
    replayed: bool


async def current(store: Store, settings: Settings, at: datetime) -> SessionState:
    """Resolve the session state at ``at``."""
    if await store.market_is_open(at):
        return SessionState(is_open=True, replayed=False)
    if not settings.replay_last_session:
        return SessionState(is_open=False, replayed=False)
    replayable = await store.last_session_close(at) is not None
    return SessionState(is_open=replayable, replayed=replayable)
