"""Runtime configuration, read from the environment.

All settings are env-driven so the same image runs unchanged in every
environment. Nothing here is business logic — only knobs the platform sets.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration.

    Values come from environment variables (case-insensitive), falling back to
    a local ``.env`` file during development.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    # HTTP server. Northflank (and most platforms) inject PORT.
    host: str = "0.0.0.0"  # noqa: S104 - containers must bind all interfaces
    port: int = Field(default=8080, ge=1, le=65535)

    # Postgres. Unset means "run without a database" (useful locally).
    database_url: str | None = None
    # Set min_size=0 against serverless Postgres that bills compute time
    # (e.g. Neon): holding an idle connection open keeps its compute awake.
    db_pool_min_size: int = Field(default=1, ge=0)
    db_pool_max_size: int = Field(default=4, ge=1)
    db_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    db_pool_max_idle_seconds: float = Field(default=120.0, gt=0)
    # Readiness must answer quickly: a hanging probe is worse than a failing
    # one, because the platform's own health check times out instead.
    db_ping_timeout_seconds: float = Field(default=3.0, gt=0)

    # Agent loop pacing.
    agent_interval_seconds: float = Field(default=30.0, gt=0)
    agent_error_backoff_seconds: float = Field(default=5.0, gt=0)
    agent_max_backoff_seconds: float = Field(default=300.0, gt=0)

    # How long to let in-flight work finish after SIGTERM.
    shutdown_grace_seconds: float = Field(default=20.0, gt=0)
