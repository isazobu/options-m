from __future__ import annotations

import asyncio

from options_m.agents import agent_interval, run_agent, run_agents
from options_m.config import Settings


class _CountingAgent:
    """Agent that records calls and optionally fails a number of times."""

    def __init__(self, name: str = "counter", fail_times: int = 0) -> None:
        self._name = name
        self.calls = 0
        self._fail_times = fail_times

    @property
    def name(self) -> str:
        return self._name

    async def step(self) -> None:
        self.calls += 1
        if self.calls <= self._fail_times:
            msg = f"boom {self.calls}"
            raise RuntimeError(msg)


def _settings(**overrides: float) -> Settings:
    base: dict[str, float] = {
        "agent_interval_seconds": 0.01,
        "agent_error_backoff_seconds": 0.01,
        "agent_max_backoff_seconds": 0.02,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def test_agent_stops_when_shutdown_is_set() -> None:
    agent = _CountingAgent()
    shutdown = asyncio.Event()
    task = asyncio.create_task(run_agent(agent, _settings(), shutdown))

    await asyncio.sleep(0.05)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1)

    assert agent.calls > 0


async def test_failing_agent_keeps_retrying_and_recovers() -> None:
    agent = _CountingAgent(fail_times=2)
    shutdown = asyncio.Event()
    task = asyncio.create_task(run_agent(agent, _settings(), shutdown))

    await asyncio.sleep(0.15)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1)

    # The loop survived both failures and kept going afterwards.
    assert agent.calls > 2


async def test_one_failing_agent_does_not_stop_its_sibling() -> None:
    healthy = _CountingAgent("healthy")
    broken = _CountingAgent("broken", fail_times=1000)
    shutdown = asyncio.Event()
    task = asyncio.create_task(run_agents([healthy, broken], _settings(), shutdown))

    await asyncio.sleep(0.1)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1)

    assert healthy.calls > 1
    assert broken.calls > 0


async def test_run_agents_waits_when_none_registered() -> None:
    shutdown = asyncio.Event()
    task = asyncio.create_task(run_agents([], _settings(), shutdown))

    shutdown.set()
    await asyncio.wait_for(task, timeout=1)


class _PacedAgent(_CountingAgent):
    """Agent that overrides the global cadence with its own."""

    @property
    def interval_seconds(self) -> float:
        return 0.5


def test_agent_without_an_interval_uses_the_global_default() -> None:
    assert agent_interval(_CountingAgent(), _settings()) == 0.01


def test_an_agent_may_set_its_own_cadence() -> None:
    assert agent_interval(_PacedAgent(), _settings()) == 0.5


async def test_a_slow_agent_does_not_starve_a_fast_sibling() -> None:
    """Each loop paces itself; one agent's cadence never gates another's."""
    fast = _CountingAgent("fast")
    slow = _PacedAgent("slow")
    shutdown = asyncio.Event()
    task = asyncio.create_task(run_agents([fast, slow], _settings(), shutdown))

    await asyncio.sleep(0.15)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2)

    assert fast.calls > slow.calls
