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

from options_m.agents.execution import ExecutionAgent
from options_m.agents.market_pulse import MarketPulseAgent
from options_m.agents.position_manager import PositionManagerAgent
from options_m.agents.reflection import ReflectionAgent
from options_m.agents.strategist import StrategistAgent
from options_m.agents.telegram_reporter import TelegramReporterAgent
from options_m.config import Settings
from options_m.lifecycle import sleep_unless_shutdown
from options_m.llm import FeatherlessLlm
from options_m.mcp_client import AlpacaMcp
from options_m.notify import Notifier, NullNotifier
from options_m.risk import RiskEngine, RiskLimits
from options_m.store import Store

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


def build_agents(
    settings: Settings,
    mcp: AlpacaMcp,
    store: Store,
    llm: FeatherlessLlm | None = None,
    notifier: Notifier | None = None,
) -> list[Agent]:
    """Construct the agents this process should run.

    Register real agents here so they receive their dependencies explicitly.
    ``llm`` is optional so callers without Featherless credentials still boot;
    StrategistAgent and ReflectionAgent check ``llm.is_enabled`` and skip
    gracefully when the model is not configured. ``notifier`` is optional for
    the same reason: without Telegram credentials the process runs with a
    :class:`~options_m.notify.NullNotifier` and the reporter agent is not
    registered at all, so it costs nothing.
    """
    risk_engine = RiskEngine(RiskLimits.from_settings(settings))
    _notifier = notifier or NullNotifier()
    _llm = llm or FeatherlessLlm(
        api_key=settings.featherless_api_key,
        base_url=settings.featherless_base_url,
        model=settings.featherless_model_deep,
        timeout_seconds=settings.llm_timeout_seconds,
        daily_token_budget=settings.llm_daily_token_budget,
    )
    agents: list[Agent] = [
        MarketPulseAgent(settings, mcp, store),
        PositionManagerAgent(settings, mcp, store, notifier=_notifier),
        ExecutionAgent(settings, mcp, store, risk_engine, notifier=_notifier),
        StrategistAgent(settings, store, _llm, notifier=_notifier),
        ReflectionAgent(settings, store, _llm),
    ]
    if not isinstance(_notifier, NullNotifier):
        agents.append(TelegramReporterAgent(settings, store, _notifier))
    return agents


async def run_agent(
    agent: Agent, settings: Settings, shutdown: asyncio.Event, *, label: str | None = None
) -> None:
    """Drive one agent until shutdown, isolating and backing off on errors.

    ``label``, when given, prefixes the logger child and the telemetry
    ``extra`` so concurrent stacks stay distinguishable in the logs. Unset
    leaves the output exactly as it was.
    """
    log = logger.getChild(f"{label}.{agent.name}" if label else agent.name)
    extra_base: dict[str, object] = {"agent": agent.name}
    if label is not None:
        extra_base["profile"] = label
    consecutive_failures = 0
    log.info(
        "agent started",
        extra={**extra_base, "interval_seconds": agent_interval(agent, settings)},
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
                    **extra_base,
                    "consecutive_failures": consecutive_failures,
                    "retry_in_seconds": delay,
                },
            )
        else:
            if consecutive_failures:
                log.info(
                    "agent recovered",
                    extra={**extra_base, "after_failures": consecutive_failures},
                )
            consecutive_failures = 0
            elapsed = time.monotonic() - started
            delay = max(0.0, agent_interval(agent, settings) - elapsed)

        await sleep_unless_shutdown(shutdown, delay)

    log.info("agent stopped", extra=extra_base)


async def run_agents(
    agents: list[Agent],
    settings: Settings,
    shutdown: asyncio.Event,
    *,
    label: str | None = None,
) -> None:
    """Run every agent concurrently until shutdown is requested.

    ``label`` is an optional identifier for this group, threaded into each
    :func:`run_agent` and the task names.
    """
    if not agents:
        logger.warning("no agents registered", extra={"profile": label} if label else None)
        await shutdown.wait()
        return

    prefix = f"agent:{label}:" if label else "agent:"
    async with asyncio.TaskGroup() as tg:
        for agent in agents:
            tg.create_task(
                run_agent(agent, settings, shutdown, label=label),
                name=f"{prefix}{agent.name}",
            )
