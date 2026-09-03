"""Runtime configuration, read from the environment.

All settings are env-driven so the same image runs unchanged in every
environment. Nothing here is business logic — only knobs the platform sets.
"""

from __future__ import annotations

from datetime import date

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
    # 0.0.0.0 is required, not a mistake: the process runs inside a container
    # and must accept traffic forwarded from the host, not just localhost.
    host: str = "0.0.0.0"  # noqa: S104
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
    alpaca_toolsets: str = "account,trading,assets,options-data,stock-data,news"
    mcp_call_timeout_seconds: float = Field(default=30.0, gt=0)
    mcp_max_retries: int = Field(default=2, ge=0)
    # Optional JSON list of runtime profiles. Unset runs a single default from
    # the keys above; see options_m.runtime. Read here so it works from .env
    # as well as from a real environment variable.
    profiles: str | None = None

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

    # IV Rank / IV percentile. Both counted in *trading days*, never in
    # readings: the pulse writes a reading a minute, so a window measured in
    # rows measures hours. 252 sessions is the trading year every desk quotes
    # IV Rank over; below 126 the store reports the rank MISSING rather than
    # ranking today's vol against a fortnight of it.
    iv_rank_window_days: int = Field(default=252, gt=1)
    iv_rank_min_days: int = Field(default=126, gt=1)
    # Reconstruct the missing part of that window from historical option bars
    # (see iv_backfill). Paced per tick because each symbol costs a handful of
    # Alpaca calls and the free tier is shared with everything else.
    iv_backfill_enabled: bool = True
    iv_backfill_symbols_per_tick: int = Field(default=1, gt=0)

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

    # Dynamic position sizing — see sizing.py. Everything here scales a trade
    # *inside* the hard limits above; max_premium_pct_per_trade remains the
    # ceiling risk.py enforces independently, and sizing clamps to it.
    #
    # The baseline fraction of equity risked on one trade, before any state
    # scaling. Set below max_premium_pct_per_trade on purpose: the scalars below
    # multiply out to roughly 3x in the best case, so the *starting* point has
    # to leave room above it for a high-conviction trade to actually grow into
    # the ceiling rather than being clamped on every single fill.
    base_risk_pct_per_trade: float = Field(default=0.015, gt=0.0, le=1.0)
    # Share of options buying power one trade may tie up. Options are never
    # marginable, so this is the real portfolio-heat meter: every open
    # defined-risk structure already holds its max loss as collateral, which
    # means buying power falling *is* aggregate open risk rising. Well below 1.0
    # so exits, adjustments and mark-to-market moves on the open book always
    # have collateral left.
    buying_power_utilization_cap: float = Field(default=0.50, gt=0.0, le=1.0)

    # Portfolio-level Greeks caps — the two things max_concurrent_positions
    # cannot see. This universe (SPY, QQQ, IWM + six large-cap tech names)
    # correlates around 0.8-0.9, and the matrix picks the same structure family
    # for all of them whenever the IV regime is the same, so five positions are
    # one bet. See exposure.py.
    #
    # Both defaults are derived from drawdown_halt_pct (0.08) rather than picked,
    # so the caps and the breaker agree on the same worst case:
    #
    #   Delta: at 1.00, a maxed book loses ~1% of equity per 1% index move, so a
    #   5% index gap costs ~5% — inside the halt. Measured on the intended
    #   5-position credit book at 10 DTE: 73% of equity, so this permits the
    #   book it was calibrated for with headroom, and blocks roughly a 1.4x one.
    #
    #   Vega: at 0.0075, a 10-point vol shock on a maxed book costs ~7.5% of
    #   equity — the halt, near enough. Measured on the same book: a credit
    #   structure's long wing offsets most of its short's vega, so a credit book
    #   sits at 0.07-0.09% and this never binds; a long-strangle book (both legs
    #   long vega, nothing offsetting) reaches 0.38-0.52%, which is what the cap
    #   is actually for. Short DTE shrinks vega — it scales with sqrt(T) — which
    #   is why at a 7-14 DTE window gamma, not vega, is the live risk.
    #
    # Both are compared on the absolute value: a book heavily short the index is
    # as directional as one heavily long, and a book short five vols is as
    # exposed as one long five.
    max_beta_weighted_delta_pct: float = Field(default=1.00, gt=0.0)
    max_net_vega_pct: float = Field(default=0.0075, gt=0.0)

    # Drawdown taper (anti-martingale, and the whole recovery mechanism). Size
    # scales *down* as equity falls away from its high-water mark, reaching this
    # floor exactly at drawdown_halt_pct. Sizing up into a drawdown is how an
    # account dies before the halt ever fires; sizing down is what keeps enough
    # capital alive to trade the recovery.
    drawdown_size_floor: float = Field(default=0.35, gt=0.0, le=1.0)
    # ...and the other direction: profits fund a bigger bet. Reaches the cap
    # once the campaign is up gain_size_reference_pct. Pressing winners with
    # house money is the only honest way to compound inside a short window.
    gain_size_cap: float = Field(default=1.60, ge=1.0)
    gain_size_reference_pct: float = Field(default=0.04, gt=0.0)

    # Conviction band → size multiplier. Conviction below conviction_floor never
    # reaches sizing (the matrix forces "hold"), so the live band is
    # [conviction_floor, 1.0] and it is mapped onto [min_mult, max_mult]:
    # bet size proportional to stated edge, a coarse discretised Kelly.
    conviction_size_min_mult: float = Field(default=0.60, gt=0.0)
    conviction_size_max_mult: float = Field(default=1.50, gt=0.0)
    # ...and how much that band is trusted. Kelly sizing wants a *calibrated*
    # probability; what conviction actually is, is a language model's
    # self-reported confidence, with no prior claim to predicting anything. The
    # measured correlation between conviction and realised P&L shrinks the
    # multiplier toward 1.0 — at zero reliability every trade is sized the same,
    # which is the correct answer when the number turns out to carry no signal.
    #
    # The prior applies until there are enough closed trades to measure. It is
    # 0.5, not 1.0: a short campaign closes something like eight trades, a
    # correlation over eight points is noise, and assuming full predictive power
    # on no evidence is the aggressive direction. Raise it to 1.0 to restore
    # unconditional trust in conviction; set it to 0.0 to size every trade the
    # same until the data earns otherwise.
    conviction_reliability_prior: float = Field(default=0.50, ge=0.0, le=1.0)
    conviction_calibration_min_samples: int = Field(default=20, ge=2)

    # Campaign horizon. Unset start date means "no horizon" — every scalar above
    # still applies, but nothing paces or closes the window. Set it to run a
    # fixed-length sprint: capital is front-loaded into the first session, and
    # new opens stop once too few sessions remain for a position to work.
    campaign_start_date: date | None = None
    campaign_days: int = Field(default=3, ge=1)
    # Neutral by default. Sizing bigger because the calendar is running out is
    # forced trading, not a professional practice: if the first session offers
    # no setup, the answer is not to trade the first session. The knob stays so
    # a deliberate front-load is possible, but it is off unless asked for.
    campaign_front_load_mult: float = Field(default=1.00, ge=1.0)
    # New opens are refused while sessions_remaining <= this. At the default a
    # position always gets at least one full session after the one it opens in:
    # opening a spread that must be flattened hours later pays the bid/ask twice
    # for whatever drift happens in between, which is not a strategy.
    campaign_min_sessions_to_hold: int = Field(default=1, ge=0)
    # On the campaign's final session, close every open structure this many
    # minutes before the cached session close. This is a real session-aware
    # flatten; exit_time_stop_days remains only a calendar-age backstop.
    campaign_flatten_minutes_before_close: int = Field(default=20, ge=0)

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
    # DeepSeek V4 Flash thinking can sit on the socket for >30s and then
    # return empty content. 90s covers one slow attempt; complete_json also
    # turns thinking off so the JSON answer is not eaten by CoT.
    llm_timeout_seconds: float = Field(default=90.0, gt=0)
    llm_max_tokens: int = Field(default=4096, gt=0)
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
    # Hard ceilings on *trade-attempt* volume over a rolling 24h window —
    # pending / submitted / rejected / filled. ``no_action`` and ``llm_failed``
    # do not count at either the per-symbol or global cap: a session of holds
    # must not silence the rest of the day. Cooldown still applies to every
    # look so the top name is not re-prompted every tick. A symbol at its
    # per-symbol cap, or the whole run at the global cap, is recorded as
    # skipped="proposal_cap".
    max_proposals_per_symbol_per_day: int = Field(default=3, ge=1)
    max_proposals_per_day: int = Field(default=40, ge=1)
    # Effective options trading level override. Normally read from the account
    # cache; this caps it (useful if the paper account auto-approves Level 3
    # but you want to test Level-2 degradation).
    options_level: int = Field(default=3, ge=1, le=3)

    # Whether the matrix may open structures paid for with a debit — the whole
    # "cheap" IV column. On by default: owning cheap volatility is a legitimate
    # answer to a cheap-volatility regime, and over a normal holding period a
    # debit vertical has room for its thesis to resolve.
    #
    # Turn it off for a campaign measured in sessions. A sold structure earns
    # from time passing, which is certain; a bought one earns from the
    # underlying moving the forecast way, which here rests on an SMA/RSI
    # classifier and a language model's self-reported confidence, neither with
    # measured predictive power (see conviction_reliability_prior). Over two or
    # three sessions that is a coin flip paying two bid/ask spreads for the
    # privilege. The cost of switching it off is real and should be expected:
    # when no name is in an expensive-IV regime the matrix holds everything and
    # the book stays flat. Flat beats a negative-expectancy position.
    allow_bought_premium: bool = True

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
    #
    # Matched to the holding period, which is the single biggest lever on a
    # short-horizon book. Measured on a real SPY 640 / IV 18% chain with this
    # module's own wing formula, holding a delta-selected credit vertical for
    # three sessions with spot flat:
    #
    #   DTE   captured   % of max profit   net of 2 round-trip spreads   on risk
    #     7    $45.3          32.8%                 $37.3                 6.42%
    #    14    $26.9          14.0%                 $18.9                 2.30%
    #    21    $20.6           8.9%                 $12.6                 1.25%
    #    30    $16.5           6.1%                 $ 8.5                 0.70%
    #
    # At 30 DTE the bid/ask takes half of what three sessions of decay pays, and
    # exit_credit_profit_target_pct (0.50) needs ~17 sessions to trigger — so a
    # short campaign would end holding structures that never reached their own
    # target. The cost of moving in is gamma: at 7-14 DTE a gap through the short
    # strike reaches max loss far faster, which is why exit_dte_short_premium
    # below has to be re-based and why sizing (not the DTE stop) becomes the
    # thing holding the risk.
    dte_target_min: int = Field(default=7, gt=0)
    dte_target_max: int = Field(default=14, gt=0)

    # Phase 4 — ReflectionAgent.
    reflection_interval_seconds: float = Field(default=3600.0, gt=0)

    # Phase 4 — StrategistAgent close-proposal thresholds (deterministic, no LLM).
    # StrategistAgent reads the positions cache and writes a close proposal when
    # any of these conditions is met; ExecutionAgent then executes it.
    # These two are the fallback pair, used for a position whose originating
    # strategy could not be resolved — the per-family thresholds below are what
    # an enriched position is actually measured against.
    exit_profit_target_pct: float = Field(default=0.50, gt=0.0, le=1.0)
    exit_stop_loss_pct: float = Field(default=0.50, gt=0.0, le=1.0)
    # Holding duration, not DTE. Cut to the campaign's own length so capital is
    # not still sitting in a structure after the window it was sized for has
    # closed. This is an approximation of an end-of-campaign flatten, not a
    # replacement for one — it counts calendar days from the fill, so a position
    # opened on the first session exits a day after a three-session campaign
    # ends rather than inside it.
    exit_time_stop_days: int = Field(default=3, ge=1)

    # Days-to-expiry exits, checked before any P&L threshold. The hard floor
    # applies to every structure — carrying an ITM option into expiration turns
    # an options position into an unwanted stock position. The short-premium
    # rule applies only to credit structures, where gamma dominates P&L in the
    # final weeks; a debit or long structure is holding convexity it paid for.
    exit_dte_hard_floor: int = Field(default=2, ge=0)
    # MUST stay below dte_target_min, or every credit structure is born inside
    # its own DTE stop and StrategistAgent proposes closing it on the first tick
    # after the fill — the system would open and immediately close, paying two
    # spreads for nothing. At a 7-14 DTE entry window the gamma regime is where
    # the book deliberately lives, so this stops being the gamma guard: the
    # per-family stop loss and the position size are.
    exit_dte_short_premium: int = Field(default=3, ge=0)

    # Per-family P&L thresholds, measured on pnl_pct (unrealized P&L over the
    # net value paid or received at entry). For a credit structure pnl_pct is
    # literally the share of the credit realised, so 0.50 is the classic "take
    # half the credit". The credit stop is expressed the same way: 1.00 means a
    # loss equal to the credit received, i.e. the spread now costs 2x what it
    # paid. Note that a wide credit (the builder allows up to
    # max_credit_width_pct of the width) can have a defined max loss smaller
    # than 1x credit, in which case the structure caps out before this stop.
    exit_credit_profit_target_pct: float = Field(default=0.50, gt=0.0, le=1.0)
    exit_credit_stop_loss_pct: float = Field(default=1.00, gt=0.0, le=10.0)
    exit_debit_profit_target_pct: float = Field(default=0.75, gt=0.0, le=10.0)
    exit_debit_stop_loss_pct: float = Field(default=0.50, gt=0.0, le=1.0)
    exit_long_profit_target_pct: float = Field(default=1.00, gt=0.0, le=10.0)
    exit_long_stop_loss_pct: float = Field(default=0.50, gt=0.0, le=1.0)
    # Working close limits are replaced on this cadence, one nudge more
    # marketable per attempt. Entries are never repriced automatically.
    close_reprice_seconds: float = Field(default=30.0, gt=0.0)
    close_reprice_max_attempts: int = Field(default=3, ge=0)

    # Telegram notifications (outbound only — no bot listener, no commands).
    # Unset token or chat id means "run without notifications", the same
    # convention as an unset DATABASE_URL or ALPACA_API_KEY above.
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_notify_decisions: bool = True
    telegram_notify_orders: bool = True
    telegram_notify_errors: bool = True
    # Cadence of the portfolio snapshot agent. Gated on market hours, so this
    # is a within-session cadence, not a wall-clock one.
    telegram_summary_interval_seconds: float = Field(default=1800.0, gt=0)
    # An error storm repeats one message thousands of times. Suppress an
    # identical message inside this window — Telegram 429s well before that.
    telegram_dedupe_seconds: float = Field(default=300.0, ge=0)
    # A bounded queue is what keeps a slow Telegram from ever reaching the
    # trading loop: past this depth the oldest message is dropped, not awaited.
    telegram_queue_max: int = Field(default=100, ge=1)
    telegram_timeout_seconds: float = Field(default=10.0, gt=0)

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
