"""Assembling the components the event loop runs.

:func:`assemble` wires the broker session, store and agent set for this
process over the shared database pool.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass

from options_m.agents import Agent, build_agents
from options_m.config import Settings
from options_m.db import Database
from options_m.llm import FeatherlessLlm
from options_m.mcp_client import AlpacaMcp
from options_m.notify import Notifier
from options_m.store import Store


@dataclass(slots=True)
class Runner:
    """A wired stack: settings, broker session, store, and agents."""

    settings: Settings
    mcp: AlpacaMcp
    store: Store
    agents: list[Agent]


async def assemble(
    settings: Settings,
    db: Database,
    llm: FeatherlessLlm | None,
    notifier: Notifier | None,
    stack: AsyncExitStack,
) -> Runner:
    """Build the broker session, store and agents this process should run.

    The broker session is entered on ``stack`` so it closes with the rest of
    the process's async context.
    """
    mcp = await stack.enter_async_context(AlpacaMcp(settings))
    store = Store(db)
    agents = build_agents(settings, mcp, store, llm, notifier=notifier)
    return Runner(settings, mcp, store, agents)
