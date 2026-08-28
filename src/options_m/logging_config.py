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
