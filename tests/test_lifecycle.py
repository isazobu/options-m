from __future__ import annotations

import asyncio
import time

from options_m.lifecycle import sleep_unless_shutdown


async def test_sleep_returns_after_full_delay_when_not_shutting_down() -> None:
    shutdown = asyncio.Event()
    started = time.monotonic()

    await sleep_unless_shutdown(shutdown, 0.05)

    assert time.monotonic() - started >= 0.04


async def test_sleep_returns_early_on_shutdown() -> None:
    shutdown = asyncio.Event()
    started = time.monotonic()

    async def _trigger() -> None:
        await asyncio.sleep(0.01)
        shutdown.set()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(_trigger())
        await sleep_unless_shutdown(shutdown, 5.0)

    assert time.monotonic() - started < 1.0
