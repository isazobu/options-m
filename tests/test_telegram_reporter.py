"""Tests for TelegramReporterAgent.

The agent reads only local caches, so every test here drives it through a real
in-memory ``Store`` (``Database`` with no URL) and a recording notifier.
"""

from __future__ import annotations

from typing import Any

from options_m.agents.telegram_reporter import TelegramReporterAgent
from options_m.config import Settings
from options_m.db import Database
from options_m.store import Store


class _Collector:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, text: str) -> None:
        self.messages.append(text)


class _OpenStore(Store):
    """A store whose market is open, or closed, on demand."""

    def __init__(self, db: Database, *, is_open: bool) -> None:
        super().__init__(db)
        self._is_open = is_open

    async def market_is_open(self, at: Any) -> bool:
        return self._is_open


def _agent(*, is_open: bool, **overrides: Any) -> tuple[TelegramReporterAgent, _Collector,
                                                        _OpenStore]:
    settings = Settings(database_url=None, dry_run=True, **overrides)
    store = _OpenStore(Database(settings), is_open=is_open)
    collector = _Collector()
    return TelegramReporterAgent(settings, store, collector), collector, store


async def test_nothing_is_sent_while_the_market_is_closed() -> None:
    agent, collector, _ = _agent(is_open=False)
    await agent.step()
    assert collector.messages == []


async def test_a_summary_is_sent_while_the_market_is_open() -> None:
    agent, collector, store = _agent(is_open=True)
    await store.upsert_position("SPY", {"market_value": 1200.0, "unrealized_pl": 50.0,
                                        "pnl_pct": 0.043})
    await agent.step()
    assert len(collector.messages) == 1
    assert "SPY" in collector.messages[0]
    assert "Pozisyon özeti" in collector.messages[0]


async def test_the_summary_is_tagged_when_dry_run() -> None:
    agent, collector, _ = _agent(is_open=True)
    await agent.step()
    assert "DRY RUN" in collector.messages[0]


async def test_the_close_fires_one_final_summary_then_goes_quiet() -> None:
    agent, collector, store = _agent(is_open=True)
    await agent.step()
    store._is_open = False
    await agent.step()
    assert "Seans kapanış özeti" in collector.messages[1]

    # ...and every subsequent closed tick stays silent.
    await agent.step()
    assert len(collector.messages) == 2


async def test_booting_after_the_close_sends_no_closing_summary() -> None:
    # The agent never observed the session, so it has no close to report.
    agent, collector, _ = _agent(is_open=False)
    await agent.step()
    await agent.step()
    assert collector.messages == []


async def test_the_agent_records_its_own_run() -> None:
    agent, _, store = _agent(is_open=True)
    await agent.step()
    runs = await store.recent_agent_runs()
    assert [r["agent"] for r in runs] == ["telegram_reporter"]
    assert runs[0]["ok"] is True


async def test_the_interval_comes_from_settings() -> None:
    agent, _, _ = _agent(is_open=True, telegram_summary_interval_seconds=42.0)
    assert agent.interval_seconds == 42.0
    assert agent.name == "telegram_reporter"


async def test_a_failure_is_recorded_and_re_raised() -> None:
    agent, _, store = _agent(is_open=True)

    async def _boom() -> list[dict[str, Any]]:
        raise RuntimeError("cache unavailable")

    store.get_cached_positions = _boom  # type: ignore[method-assign]
    try:
        await agent.step()
    except RuntimeError:
        pass
    else:  # pragma: no cover - the agent must propagate
        raise AssertionError("step() should have re-raised")
    runs = await store.recent_agent_runs()
    assert runs[0]["ok"] is False
    assert "cache unavailable" in str(runs[0]["error"])
