"""Process lifecycle: signal handling and interruptible sleeping.

Platforms stop containers by sending SIGTERM and then killing the process a
short while later. Catching it is what lets in-flight work finish cleanly
instead of being severed mid-iteration.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from types import FrameType

logger = logging.getLogger(__name__)


def _stop_signals() -> tuple[signal.Signals, ...]:
    """Signals that mean "stop", including the Windows-only SIGBREAK."""
    signals = [signal.SIGINT, signal.SIGTERM]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:  # Windows: Ctrl+Break / CTRL_BREAK_EVENT
        signals.append(sigbreak)
    return tuple(signals)


def install_signal_handlers(shutdown: asyncio.Event) -> None:
    """Set ``shutdown`` when the process is asked to stop.

    Uses the event loop's signal support where available (POSIX) and falls
    back to :func:`signal.signal` on platforms without it (Windows).
    """
    loop = asyncio.get_running_loop()

    def _request_shutdown(signame: str) -> None:
        if shutdown.is_set():
            logger.warning("shutdown already in progress", extra={"signal": signame})
            return
        logger.info("shutdown requested", extra={"signal": signame})
        shutdown.set()

    for sig in _stop_signals():
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except NotImplementedError:
            # Windows: no loop-level signal support, use the sync handler.
            def _handler(
                signum: int, _frame: FrameType | None, *, _loop: asyncio.AbstractEventLoop = loop
            ) -> None:
                _loop.call_soon_threadsafe(_request_shutdown, signal.Signals(signum).name)

            signal.signal(sig, _handler)


async def sleep_unless_shutdown(shutdown: asyncio.Event, delay: float) -> None:
    """Sleep for ``delay`` seconds, returning early if shutdown is requested."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(shutdown.wait(), timeout=delay)
