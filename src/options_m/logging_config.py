"""Application logging setup.

Stdlib-only, no business logic. Import :func:`setup_logging` once at the
process entry point, then use ``logging.getLogger(__name__)`` everywhere else.

Environment variables:
    LOG_LEVEL   Root log level (default: INFO).
    LOG_FORMAT  ``json`` for machine-readable output, ``text`` for humans
                (default: json).
"""

from __future__ import annotations

import json
import logging
import logging.config
import os
import sys
from datetime import UTC, datetime
from typing import Any, Final

_RESERVED: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "taskName"}

_TEXT_FORMAT: Final[str] = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Every list-returning Alpaca tool logs one FastMCP ERROR per call, always.
#
# The tool's meta carries wrap_result=true, so the client looks for the payload
# under a top-level "result" key. Alpaca's TrustBoundaryMiddleware has already
# rewritten the structured content to {"_alpaca_mcp_security": ..., "data": ...},
# which has no "result" key — so the lookup yields None, validating None against
# the declared dict[str, Any] fails, and the client logs at ERROR. The middleware
# and the wrap metadata simply disagree.
#
# It is structural, not incidental: it fires on every call to get_all_positions,
# get_orders, get_calendar and friends, whether the list is empty or full. That
# is several ERROR lines a minute forever, which buries real failures and trips
# alerting.
_FASTMCP_TOOL_LOGGER: Final[str] = "fastmcp.client.mixins.tools"
_PARSE_ERROR_MARKER: Final[str] = "Error parsing structured content"


class _RepeatedParseErrorFilter(logging.Filter):
    """Let the first of each distinct parse error through, drop its repeats.

    Suppressing this outright would be the wrong trade in a trading service:
    a filter that deletes ERRORs is a filter that can hide a real one. So
    nothing is hidden here — the first occurrence of each distinct message is
    logged at ERROR exactly as before, and only *identical repeats* are
    dropped. One line tells you the condition exists; the ten-thousandth adds
    nothing.

    A genuinely different parse failure carries a different message, so it gets
    its own first-occurrence at ERROR. Only messages containing
    ``Error parsing structured content`` are considered at all; everything else
    from this logger passes untouched.

    Why the dropped repeats cost nothing: ``AlpacaMcp._as_json`` reads
    ``result.structured_content`` and unwraps the security envelope itself. It
    never touches the ``data`` field this failed validation would have
    populated — verified against get_clock, get_account_info,
    get_account_config, get_all_positions and get_orders. **If a call site ever
    starts reading ``CallToolResult.data``, revisit this.**
    """

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[str] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if _PARSE_ERROR_MARKER not in message:
            return True
        if message in self._seen:
            return False
        self._seen.add(message)
        return True


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON for log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # Anything passed via `logger.info("...", extra={...})`.
        payload.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(level: str | None = None, *, fmt: str | None = None) -> None:
    """Configure the root logger. Safe to call once at startup.

    Args:
        level: Log level name. Falls back to ``$LOG_LEVEL``, then ``INFO``.
        fmt: ``json`` or ``text``. Falls back to ``$LOG_FORMAT``, then ``json``.
    """
    resolved_level = (level if level else os.getenv("LOG_LEVEL", "INFO")).upper()
    resolved_fmt = (fmt if fmt else os.getenv("LOG_FORMAT", "json")).lower()

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {"()": f"{__name__}.JsonFormatter"},
                "text": {"format": _TEXT_FORMAT},
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "formatter": "json" if resolved_fmt == "json" else "text",
                    "stream": sys.stdout,
                }
            },
            "root": {"level": resolved_level, "handlers": ["stdout"]},
        }
    )
    logging.captureWarnings(capture=True)
    install_mcp_noise_filter()


def install_mcp_noise_filter() -> None:
    """Attach the repeat filter, without stacking duplicates on re-setup."""
    logger = logging.getLogger(_FASTMCP_TOOL_LOGGER)
    if any(isinstance(existing, _RepeatedParseErrorFilter) for existing in logger.filters):
        return
    logger.addFilter(_RepeatedParseErrorFilter())
