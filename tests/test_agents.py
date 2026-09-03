from __future__ import annotations

import asyncio
import logging

import pytest

from options_m.agents import agent_interval, build_agents, run_agent, run_agents
from options_m.config import Settings
from options_m.db import Database
from options_m.mcp_client import AlpacaMcp
from options_m.notify import NullNotifier
from options_m.store import Store


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


async def test_a_label_namespaces_the_agent_logger(caplog: pytest.LogCaptureFixture) -> None:
    """A label prefixes the logger child and the telemetry extra."""
    agent = _CountingAgent("market_pulse")
    shutdown = asyncio.Event()
    with caplog.at_level(logging.INFO, logger="options_m.agents"):
        task = asyncio.create_task(run_agent(agent, _settings(), shutdown, label="b"))
        await asyncio.sleep(0.03)
        shutdown.set()
        await asyncio.wait_for(task, timeout=1)

    records = [r for r in caplog.records if r.name == "options_m.agents.b.market_pulse"]
    assert records, "expected log records under the labelled child logger"
    assert any(getattr(r, "profile", None) == "b" for r in records)


async def test_no_label_keeps_the_original_logger_name(caplog: pytest.LogCaptureFixture) -> None:
    agent = _CountingAgent("market_pulse")
    shutdown = asyncio.Event()
    with caplog.at_level(logging.INFO, logger="options_m.agents"):
        task = asyncio.create_task(run_agent(agent, _settings(), shutdown))
        await asyncio.sleep(0.03)
        shutdown.set()
        await asyncio.wait_for(task, timeout=1)

    assert any(r.name == "options_m.agents.market_pulse" for r in caplog.records)


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


# ---------------------------------------------------------------------------
# build_agents registration
# ---------------------------------------------------------------------------


def _built(**overrides: object) -> list[str]:
    settings = Settings(database_url=None, **overrides)  # type: ignore[arg-type]
    store = Store(Database(settings))
    agents = build_agents(settings, AlpacaMcp(settings), store)
    return [agent.name for agent in agents]


def test_the_reporter_is_not_registered_without_telegram() -> None:
    """A null notifier means the reporter would only ever discard its work."""
    assert "telegram_reporter" not in _built()


def test_the_reporter_is_registered_when_telegram_is_configured() -> None:
    settings = Settings(
        database_url=None, telegram_bot_token="1:A", telegram_chat_id="-1"
    )
    store = Store(Database(settings))
    from options_m.notify import build_notifier

    names = [
        agent.name
        for agent in build_agents(
            settings, AlpacaMcp(settings), store, notifier=build_notifier(settings)
        )
    ]
    assert "telegram_reporter" in names


def test_an_explicit_null_notifier_still_skips_the_reporter() -> None:
    settings = Settings(database_url=None)
    store = Store(Database(settings))
    agents = build_agents(settings, AlpacaMcp(settings), store, notifier=NullNotifier())
    assert "telegram_reporter" not in [agent.name for agent in agents]
