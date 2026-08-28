"""Process entry point.

Runs the agent loops and the admin HTTP server side by side in one asyncio
event loop, and shuts both down together on SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging

from options_m import __version__
from options_m.agents import build_agents, run_agents
from options_m.api import create_app
from options_m.config import Settings
from options_m.db import Database
from options_m.lifecycle import install_signal_handlers
from options_m.logging_config import setup_logging
from options_m.server import build_server, serve

logger = logging.getLogger(__name__)


async def run(settings: Settings) -> None:
    """Start every component and block until shutdown completes."""
    shutdown = asyncio.Event()
    install_signal_handlers(shutdown)

    logger.info(
        "application starting",
        extra={"version": __version__, "port": settings.port},
    )

    async with Database(settings) as db:
        agents = build_agents(settings, db)
        server = build_server(create_app(db, agents), settings)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(serve(server, shutdown, settings), name="http")
            tg.create_task(run_agents(agents, settings, shutdown), name="agents")

    logger.info("application stopped")


def main() -> int:
    settings = Settings()
    setup_logging(settings.log_level, fmt=settings.log_format)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("interrupted")
    except Exception:
        logger.exception("application failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
