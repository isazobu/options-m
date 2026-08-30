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
    host: str = "0.0.0.0"
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
    # Every toolset the agents actually need, and nothing else. The names come
    # from the MCP server's own toolsets.py — an unrecognised one is silently
    # ignored rather than rejected, so a typo here removes tools without any
    # error. `trading` is the one that carries positions, orders,
    # place_option_order and close_position: without it the service connects,
    # reports healthy, and cannot trade.
    alpaca_toolsets: str = "account,trading,assets,options-data,stock-data"
    mcp_call_timeout_seconds: float = Field(default=30.0, gt=0)
    mcp_max_retries: int = Field(default=2, ge=0)

    # Local market-calendar cache (2026-08-29 design change): MarketPulseAgent
    # fetches get_calendar once for this forward window and refreshes once the
    # cached window shrinks under the margin, instead of calling get_clock on
    # every agent iteration across every agent.
    market_calendar_horizon_days: int = Field(default=400, gt=0)
    market_calendar_refresh_margin_days: int = Field(default=30, gt=0)
    # How far back the cached window reaches. A cache that begins at today
    # holds no session at all over a weekend, so "when did the market last
    # trade" has no answer — which is exactly what replay_last_session needs.
    market_calendar_lookback_days: int = Field(default=7, gt=0)

    # Trading universe and safety switches.
    universe: str = "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMD,TSLA,META,GOOGL"
    # While true, no write tool can reach Alpaca. Enforced in the MCP
    # transport, not at call sites, so one forgotten call site cannot trade.
    dry_run: bool = True
    # Env-level halt. The kill_switch table is checked in addition to this.
    kill_switch: bool = False
    # Testing only. Out of hours every agent short-circuits at its first
    # market-open check, so the autonomous chain can never be exercised on a
    # weekend or overnight. This replays the most recent *real* session from
    # the market_calendar cache instead — it cannot invent one. The evidence it
    # then reasons over is by construction last session's, so startup refuses
    # to run it with dry_run disabled. See session.py.
    replay_last_session: bool = False

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
    # A relative spread cap alone disqualifies every cheap wing: a 0.10/0.15
    # quote is 40% wide but costs five cents to cross, and those wings are
    # exactly what makes an iron condor defined-risk. A leg is refused only
    # when it is wide in *both* senses — percentage and absolute.
    max_spread_abs: float = Field(default=0.05, ge=0.0)
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
    # A symbol that produced any proposal (any status) within this window is
    # skipped by candidate selection, so the highest-scoring name is not
    # re-proposed on every strategist tick. Set well above
    # strategist_interval_seconds.
    proposal_cooldown_seconds: float = Field(default=3600.0, gt=0)
    # Hard ceilings on proposal volume over a rolling 24h window — a backstop
    # for the cooldown, and until the token budget is enforced the only real
    # cap on LLM spend. A symbol at its per-symbol cap, or the whole run at the
    # global cap, is recorded as skipped="proposal_cap".
    max_proposals_per_symbol_per_day: int = Field(default=3, ge=1)
    max_proposals_per_day: int = Field(default=40, ge=1)
    # Effective options trading level override. Normally read from the account
    # cache; this caps it (useful if the paper account auto-approves Level 3
    # but you want to test Level-2 degradation).
    options_level: int = Field(default=3, ge=1, le=3)

    # Per-structure defaults consumed by matrix.py.
    short_delta_default: float = Field(default=0.25, gt=0.0, lt=1.0)
    # Wing distance as a multiple of the expected move over the option's life
    # (spot x IV x sqrt(dte/365)). A flat dollar width is only ever right for
    # one underlying at one vol level: $5 wings are a fifth of the expected
    # move on SPY at 769 — which is what made a 5-wide at-the-money iron
    # butterfly collect 95.7% of its width and read as an almost certain max
    # loss — and several times the expected move on a $30 name. Set to 0 to
    # disable scaling and fall back to spread_width_default.
    # Measured on real SPY and NVDA chains at 21-38 DTE: at 0.40-0.50 the
    # credit verticals land at 15-19% of width and the iron condor at 33%,
    # comfortably inside the credit band, on both underlyings. Above ~1.0 they
    # fall through the thin-credit floor.
    spread_width_expected_move_mult: float = Field(default=0.45, ge=0.0)
    # An at-the-money short collects far more premium than a 0.25-delta one,
    # so an iron butterfly needs much wider wings to leave a profit zone at
    # all: the same measurement put a 1-expected-move butterfly at 60-65% of
    # width and a 1.25 one at 53-57%, while anything under 0.75 was refused as
    # credit_too_rich. One multiplier cannot serve both families.
    spread_width_expected_move_mult_atm: float = Field(default=1.25, ge=0.0)
    # Only used when the scaling above is disabled, or when a caller pins a
    # width explicitly (the CLI's --spread-width).
    spread_width_default: float = Field(default=5.0, gt=0.0)
    # Structure quality floors, enforced in strategy_builder when the plan is
    # assembled. Measured credit/width by short delta, on a 38-day chain —
    # width barely moves this ratio, short delta is the lever:
    #   0.15 -> ~10%   0.20 -> ~14%   0.25 -> ~18%   0.30 -> ~21%   0.35 -> ~27%
    # The floor is 12%, not 15%: at 15% the calibrated 0.20-delta setup is
    # unreachable. For a fatter credit raise short_delta_default, not the wing.
    min_credit_width_pct: float = Field(default=0.12, gt=0.0, le=1.0)
    # ...and the ceiling, which catches the opposite failure. Credit/width is
    # roughly the risk-neutral probability of being breached, so a structure
    # collecting most of its width has almost no profit zone left: measured on
    # a real SPY chain, a 5-wide at-the-money iron butterfly collected 95.7% of
    # width, leaving a $21.75 max loss and a profit zone of spot ±$0.22 — an
    # almost certain max-loss trade that position sizing then scaled to 91
    # contracts precisely *because* the loss per contract looked tiny. Every
    # legitimate structure sits far below this: credit verticals reach 27% at
    # 0.35 delta, and an iron condor roughly doubles that over one width.
    max_credit_width_pct: float = Field(default=0.70, gt=0.0, le=1.0)
    # Debit verticals: never pay more than this share of the width, and never
    # take a structure whose best case does not at least match its worst.
    max_debit_width_pct: float = Field(default=0.45, gt=0.0, le=1.0)
    min_reward_risk: float = Field(default=1.0, ge=0.0)
    # DTE window for new structures (distinct from risk_dte_min/max which are
    # the hard account-wide bounds risk.py enforces regardless of intent).
    dte_target_min: int = Field(default=21, gt=0)
    dte_target_max: int = Field(default=38, gt=0)

    # Phase 4 — ReflectionAgent.
    reflection_interval_seconds: float = Field(default=3600.0, gt=0)

    # Phase 4 — StrategistAgent close-proposal thresholds (deterministic, no LLM).
    # StrategistAgent reads the positions cache and writes a close proposal when
    # any of these conditions is met; ExecutionAgent then executes it.
    exit_profit_target_pct: float = Field(default=0.50, gt=0.0, le=1.0)
    exit_stop_loss_pct: float = Field(default=0.50, gt=0.0, le=1.0)
    exit_time_stop_days: int = Field(default=30, ge=1)

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
