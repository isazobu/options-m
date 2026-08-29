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
from options_m.lifecycle import sleep_unless_shutdown
from options_m.mcp_client import AlpacaMcp
from options_m.risk import RiskEngine, RiskLimits
from options_m.store import Store
from options_m.trading.execution import ExecutionAgent
from options_m.trading.market_pulse import MarketPulseAgent
from options_m.trading.position_manager import PositionManagerAgent

logger = logging.getLogger(__name__)


@runtime_checkable
class Agent(Protocol):
    """One unit of autonomous work, executed repeatedly."""

    @property
    def name(self) -> str:
        """Stable identifier, used for logging and status reporting."""

    async def step(self) -> None:
        """Perform a single iteration. Raising is safe; it will be retried."""


def agent_interval(agent: Agent, settings: Settings) -> float:
    """How long to wait between iterations of ``agent``.

    Agents run at very different cadences — market telemetry every minute, an
    LLM deliberation every several minutes — so each may expose its own
    ``interval_seconds``. Those that do not fall back to the global default.
    """
    interval = getattr(agent, "interval_seconds", None)
    if isinstance(interval, int | float) and interval > 0:
        return float(interval)
    return settings.agent_interval_seconds


def build_agents(settings: Settings, mcp: AlpacaMcp, store: Store) -> list[Agent]:
    """Construct the agents this process should run.

    Register real agents here so they receive their dependencies explicitly.
    """
    risk_engine = RiskEngine(RiskLimits.from_settings(settings))
    return [
        MarketPulseAgent(settings, mcp, store),
        PositionManagerAgent(settings, mcp, store),
        ExecutionAgent(settings, mcp, store, risk_engine),
    ]


async def run_agent(agent: Agent, settings: Settings, shutdown: asyncio.Event) -> None:
    """Drive one agent until shutdown, isolating and backing off on errors."""
    log = logger.getChild(agent.name)
    consecutive_failures = 0
    log.info(
        "agent started",
        extra={"agent": agent.name, "interval_seconds": agent_interval(agent, settings)},
    )

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
            delay = max(0.0, agent_interval(agent, settings) - elapsed)

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
