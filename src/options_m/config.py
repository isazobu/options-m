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
    execution_agent_interval_seconds: float = Field(default=30.0, gt=0)
    position_manager_interval_seconds: float = Field(default=60.0, gt=0)

    # Alpaca, reached only through its official MCP server (spawned as a stdio
    # subprocess). Unset keys mean "run without a broker session", mirroring
    # how DATABASE_URL being unset means "run without a database".
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    # Paper-only by design, and not really a knob: the MCP transport pins
    # ALPACA_PAPER_TRADE="true" for the server regardless, and setting this
    # False makes startup fail rather than arming live agents.
    alpaca_paper_trade: bool = True
    alpaca_toolsets: str = "account,agents,assets,options-data,stock-data"
    mcp_call_timeout_seconds: float = Field(default=30.0, gt=0)
    mcp_max_retries: int = Field(default=2, ge=0)

    # Local market-calendar cache (2026-08-29 design change): MarketPulseAgent
    # fetches get_calendar once for this forward window and refreshes once the
    # cached window shrinks under the margin, instead of calling get_clock on
    # every agent iteration across every agent.
    market_calendar_horizon_days: int = Field(default=400, gt=0)
    market_calendar_refresh_margin_days: int = Field(default=30, gt=0)

    # Trading universe and safety switches.
    universe: str = "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMD,TSLA,META,GOOGL"
    # While true, no write tool can reach Alpaca. Enforced in the MCP
    # transport, not at call sites, so one forgotten call site cannot trade.
    dry_run: bool = True
    # Env-level halt. The kill_switch table is checked in addition to this.
    kill_switch: bool = False

    # How long to let in-flight work finish after SIGTERM.
    shutdown_grace_seconds: float = Field(default=20.0, gt=0)

    # Strategy construction.
    risk_free_rate: float = Field(default=0.045, ge=0)  # Black-Scholes delta fallback
    limit_price_spread_nudge_pct: float = Field(default=0.25, ge=0.0, le=1.0)
    standard_monthly_expiry_preference: bool = True

    # Risk limits. Single source of truth: strategy_builder's liquidity gate
    # and risk.py's account-wide gate both read these fields.
    max_premium_pct_per_trade: float = Field(default=0.02, gt=0.0, le=1.0)
    max_total_premium_pct: float = Field(default=0.15, gt=0.0, le=1.0)
    max_concurrent_positions: int = Field(default=5, ge=1)
    max_positions_per_underlying: int = Field(default=1, ge=1)
    # Deliberately distinct from StrategyIntent.dte_min/dte_max: the intent's
    # window is what a proposal *requests*; these are the hard account-wide
    # bounds risk.py enforces regardless of what was requested.
    risk_dte_min: int = Field(default=7, ge=0)
    risk_dte_max: int = Field(default=45, ge=1)
    min_open_interest: int = Field(default=100, ge=0)
    max_spread_pct: float = Field(default=0.10, gt=0.0, le=1.0)
    daily_loss_halt_pct: float = Field(default=0.03, gt=0.0, le=1.0)
    drawdown_halt_pct: float = Field(default=0.08, gt=0.0, le=1.0)
    minutes_before_close_blackout: int = Field(default=15, ge=0)

    # Dashboard access. A single shared secret, deliberately simpler than a
    # real user-auth system: this guards a judge-facing demo, not a
    # multi-tenant product. Unset means the guarded routes stay open, which
    # only matters for local/dev use — it must be set wherever the API is
    # reachable from the public internet.
    admin_token: str | None = None
    # Comma-separated browser origins allowed to call the guarded /api/*
    # routes (the separate Next.js dashboard's dev and deployed URLs).
    cors_allowed_origins: str = ""

    # Featherless, reached over plain HTTPS (OpenAI-compatible chat/completions).
    featherless_api_key: str | None = None
    featherless_base_url: str = "https://api.featherless.ai/v1"
    # Never hardcode a model id in source — it must be free to swap by env.
    featherless_chat_model: str = ""
    # Phase 3: StrategistAgent uses a separate deep-tier model (one call per
    # iteration). There is no fast-tier analyst crew — one model only.
    featherless_model_deep: str = ""
    chat_max_tool_calls: int = Field(default=4, ge=0)
    chat_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_max_tokens: int = Field(default=1024, gt=0)
    # Soft daily token ceiling — only halts StrategistAgent, never
    # PositionManagerAgent (exits must always work).
    llm_daily_token_budget: int = Field(default=100_000, gt=0)

    # Phase 3 — StrategistAgent cadence and decision thresholds.
    strategist_interval_seconds: float = Field(default=300.0, gt=0)
    # Conviction below this floor forces "hold" even when the matrix would
    # otherwise produce a structure.
    conviction_floor: float = Field(default=0.55, ge=0.0, le=1.0)
    # Effective options trading level override. Normally read from the account
    # cache; this caps it (useful if the paper account auto-approves Level 3
    # but you want to test Level-2 degradation).
    options_level: int = Field(default=3, ge=1, le=3)

    # Per-structure defaults consumed by matrix.py.
    short_delta_default: float = Field(default=0.25, gt=0.0, lt=1.0)
    spread_width_default: float = Field(default=5.0, gt=0.0)
    # DTE window for new structures (distinct from risk_dte_min/max which are
    # the hard account-wide bounds risk.py enforces regardless of intent).
    dte_target_min: int = Field(default=21, gt=0)
    dte_target_max: int = Field(default=38, gt=0)

    # Phase 4 — ReflectionAgent.
    reflection_interval_seconds: float = Field(default=3600.0, gt=0)

    @property
    def cors_origins(self) -> tuple[str, ...]:
        """The configured CORS origins, de-duplicated and order-preserving."""
        seen: dict[str, None] = {}
        for raw in self.cors_allowed_origins.split(","):
            origin = raw.strip()
            if origin:
                seen[origin] = None
        return tuple(seen)

    @property
    def universe_symbols(self) -> tuple[str, ...]:
        """The configured universe as de-duplicated uppercase symbols."""
        seen: dict[str, None] = {}
        for raw in self.universe.split(","):
            symbol = raw.strip().upper()
            if symbol:
                seen[symbol] = None
        return tuple(seen)
