"""Uvicorn server wired into the shared shutdown signal."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator

import uvicorn
from fastapi import FastAPI

from options_m.config import Settings

logger = logging.getLogger(__name__)


class _ManagedServer(uvicorn.Server):
    """Uvicorn server that leaves signal handling to the application.

    By default uvicorn installs its own SIGINT/SIGTERM handlers, which would
    replace ours and stop only the HTTP server — the agent loops would keep
    running until the platform force-killed them.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    def install_signal_handlers(self) -> None:
        return


def build_server(app: FastAPI, settings: Settings) -> uvicorn.Server:
    """Create a uvicorn server that logs through our own logging config."""
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        # The root logger already formats and ships everything.
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=int(settings.shutdown_grace_seconds),
    )
    return _ManagedServer(config)


async def serve(server: uvicorn.Server, shutdown: asyncio.Event, settings: Settings) -> None:
    """Run the HTTP server until ``shutdown`` is set."""

    async def _stop_on_shutdown() -> None:
        await shutdown.wait()
        logger.info("stopping http server")
        server.should_exit = True

    logger.info("http server starting", extra={"host": settings.host, "port": settings.port})
    async with asyncio.TaskGroup() as tg:
        stopper = tg.create_task(_stop_on_shutdown(), name="http:stopper")
        try:
            await server.serve()
        finally:
            # If serve() returned on its own (e.g. bind failure), bring the
            # rest of the process down with it rather than hanging.
            shutdown.set()
            await stopper
    logger.info("http server stopped")
