"""Process entry point.

Runs the agent loops and the admin HTTP server side by side in one asyncio
event loop, and shuts both down together on SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack

from options_m import __version__, migrate
from options_m.agents import run_agents
from options_m.api import create_app
from options_m.config import Settings
from options_m.db import Database
from options_m.lifecycle import install_signal_handlers
from options_m.llm import FeatherlessLlm
from options_m.logging_config import setup_logging
from options_m.mcp_client import LiveTradingRefused, assert_paper_intent
from options_m.notify import (
    TelegramNotifier,
    build_notifier,
    install_error_notifier,
    remove_error_notifier,
)
from options_m.runtime import assemble
from options_m.server import build_server, serve

logger = logging.getLogger(__name__)


async def run(settings: Settings) -> None:
    """Start every component and block until shutdown completes."""
    shutdown = asyncio.Event()
    install_signal_handlers(shutdown)

    logger.info(
        "application starting",
        extra={
            "version": __version__,
            "port": settings.port,
            "dry_run": settings.dry_run,
            "paper": settings.alpaca_paper_trade,
            "replay_last_session": settings.replay_last_session,
        },
    )
    if settings.replay_last_session:
        logger.warning(
            "REPLAY_LAST_SESSION is on: agents are treating the last completed session as "
            "current. Every decision is built on stale data and no order can be submitted."
        )

    # Both dependencies tolerate being unconfigured, so the process still boots
    # without credentials and reports the gap on /ready instead of crash-looping.
    llm = FeatherlessLlm(
        api_key=settings.featherless_api_key,
        base_url=settings.featherless_base_url,
        model=settings.featherless_model_deep,
        timeout_seconds=settings.llm_timeout_seconds,
        daily_token_budget=settings.llm_daily_token_budget,
    )

    # Started before the database and the broker so a failure in either is
    # itself reportable. Errors are bridged in only after setup_logging has
    # run, because dictConfig replaces the root handlers wholesale.
    notifier = build_notifier(settings)
    if isinstance(notifier, TelegramNotifier):
        await notifier.start()
    error_handler = (
        install_error_notifier(notifier, dry_run=settings.dry_run)
        if settings.telegram_notify_errors
        else None
    )
    notifier.notify(
        f"🟢 *options\\-m started* — v`{__version__}` "
        f"\\(dry\\_run\\={str(settings.dry_run).lower()}\\)"
    )

    try:
        async with Database(settings) as db, AsyncExitStack() as stack:
            await migrate.apply(db)
            runner = await assemble(settings, db, llm, notifier, stack)

            server = build_server(
                create_app(
                    db,
                    runner.agents,
                    mcp=runner.mcp,
                    store=runner.store,
                    settings=settings,
                ),
                settings,
            )

            async with asyncio.TaskGroup() as tg:
                tg.create_task(serve(server, shutdown, settings), name="http")
                tg.create_task(
                    run_agents(runner.agents, runner.settings, shutdown), name="agents"
                )
    finally:
        # Detach the bridge first: shutdown-path errors must not queue messages
        # onto a notifier that is already draining for the last time.
        remove_error_notifier(error_handler)
        notifier.notify("🔴 *options\\-m stopped*")
        if isinstance(notifier, TelegramNotifier):
            await notifier.aclose()

    logger.info("application stopped")


def main() -> int:
    # Before configuration is even parsed. A value like "true " (which the MCP
    # server does not strip, and so reads as live) would otherwise surface as an
    # opaque pydantic bool-parsing error instead of the actual danger.
    try:
        assert_paper_intent()
    except LiveTradingRefused as exc:
        logging.basicConfig(level=logging.ERROR)
        logger.error("refusing to start: %s", exc)
        return 1

    settings = Settings()
    setup_logging(settings.log_level, fmt=settings.log_format)

    # A replayed session reasons over the previous session's bars and chain.
    # That is fine for exercising the pipeline and unacceptable for order
    # entry, so the two are interlocked here rather than trusted to whoever
    # edits .env next.
    if settings.replay_last_session and not settings.dry_run:
        logger.error(
            "refusing to start: REPLAY_LAST_SESSION replays a completed session, so every "
            "decision is built on stale market data. It requires DRY_RUN=true."
        )
        return 1

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
