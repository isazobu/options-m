"""Agent supervision.

Each agent runs its own independent loop. A failing agent must never take the
process down or stop its siblings, so every iteration is isolated and retried
with exponential backoff.

Put real agents in their own modules and register them in
:func:`build_agents` — this module stays free of business logic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol, runtime_checkable

from options_m.config import Settings
from options_m.db import Database
from options_m.lifecycle import sleep_unless_shutdown

logger = logging.getLogger(__name__)


@runtime_checkable
class Agent(Protocol):
    """One unit of autonomous work, executed repeatedly."""

    @property
    def name(self) -> str:
        """Stable identifier, used for logging and status reporting."""

    async def step(self) -> None:
        """Perform a single iteration. Raising is safe; it will be retried."""


class HeartbeatAgent:
    """Placeholder agent that proves the loop is alive.

    Replace with real agents; this exists so the skeleton runs end to end.
    """

    def __init__(self, name: str = "heartbeat") -> None:
        self._name = name
        self._iterations = 0

    @property
    def name(self) -> str:
        return self._name

    async def step(self) -> None:
        self._iterations += 1
        logger.info("heartbeat", extra={"agent": self._name, "iteration": self._iterations})


def build_agents(settings: Settings, db: Database) -> list[Agent]:
    """Construct the agents this process should run.

    Register real agents here so they receive their dependencies explicitly.
    """
    del settings, db  # Placeholder agents need neither yet.
    return [HeartbeatAgent()]


async def run_agent(agent: Agent, settings: Settings, shutdown: asyncio.Event) -> None:
    """Drive one agent until shutdown, isolating and backing off on errors."""
    log = logger.getChild(agent.name)
    consecutive_failures = 0
    log.info("agent started", extra={"agent": agent.name})

    while not shutdown.is_set():
        started = time.monotonic()
        try:
            await agent.step()
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            delay = min(
                settings.agent_error_backoff_seconds * 2 ** (consecutive_failures - 1),
                settings.agent_max_backoff_seconds,
            )
            log.exception(
                "agent step failed",
                extra={
                    "agent": agent.name,
                    "consecutive_failures": consecutive_failures,
                    "retry_in_seconds": delay,
                },
            )
        else:
            if consecutive_failures:
                log.info(
                    "agent recovered",
                    extra={"agent": agent.name, "after_failures": consecutive_failures},
                )
            consecutive_failures = 0
            elapsed = time.monotonic() - started
            delay = max(0.0, settings.agent_interval_seconds - elapsed)

        await sleep_unless_shutdown(shutdown, delay)

    log.info("agent stopped", extra={"agent": agent.name})


async def run_agents(agents: list[Agent], settings: Settings, shutdown: asyncio.Event) -> None:
    """Run every agent concurrently until shutdown is requested."""
    if not agents:
        logger.warning("no agents registered")
        await shutdown.wait()
        return

    async with asyncio.TaskGroup() as tg:
        for agent in agents:
            tg.create_task(run_agent(agent, settings, shutdown), name=f"agent:{agent.name}")
