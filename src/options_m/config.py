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

    # HTTP server. Render (like most platforms) injects PORT.
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

    # Per-agent cadence. Agents that expose `interval_seconds` use their own
    # value; everything else falls back to agent_interval_seconds above.
    market_pulse_interval_seconds: float = Field(default=60.0, gt=0)

    # Alpaca, reached only through its official MCP server (spawned as a stdio
    # subprocess). Unset keys mean "run without a broker session", mirroring
    # how DATABASE_URL being unset means "run without a database".
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    # Paper-only by design, and not really a knob: the MCP transport pins
    # ALPACA_PAPER_TRADE="true" for the server regardless, and setting this
    # False makes startup fail rather than arming live trading.
    alpaca_paper_trade: bool = True
    alpaca_toolsets: str = "account,trading,assets,options-data,stock-data,news"
    mcp_call_timeout_seconds: float = Field(default=30.0, gt=0)
    mcp_max_retries: int = Field(default=2, ge=0)

    # Trading universe and safety switches.
    universe: str = "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMD,TSLA,META,GOOGL"
    # While true, no write tool can reach Alpaca. Enforced in the MCP
    # transport, not at call sites, so one forgotten call site cannot trade.
    dry_run: bool = True
    # Env-level halt. The kill_switch table is checked in addition to this.
    kill_switch: bool = False

    # How long to let in-flight work finish after SIGTERM.
    shutdown_grace_seconds: float = Field(default=20.0, gt=0)

    @property
    def universe_symbols(self) -> tuple[str, ...]:
        """The configured universe as de-duplicated uppercase symbols."""
        seen: dict[str, None] = {}
        for raw in self.universe.split(","):
            symbol = raw.strip().upper()
            if symbol:
                seen[symbol] = None
        return tuple(seen)
