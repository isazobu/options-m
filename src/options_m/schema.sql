-- Schema for options-m. Applied at startup, idempotently, in one transaction.
--
-- Rules that hold across every table here:
--   * timestamps are timestamptz, defaulting to now()
--   * money is numeric, never double precision — float rounding has no place
--     anywhere near a P/L figure
--   * later phases add tables and columns; nothing here is ever rewritten;
--     new columns on existing tables use ALTER TABLE ... ADD COLUMN IF NOT EXISTS

CREATE TABLE IF NOT EXISTS agent_runs (
    id          bigserial PRIMARY KEY,
    agent       text        NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    duration_ms integer     NOT NULL,
    ok          boolean     NOT NULL,
    error       text,
    detail      jsonb
);

CREATE INDEX IF NOT EXISTS agent_runs_agent_started_idx
    ON agent_runs (agent, started_at DESC);

-- One row per market-pulse iteration. The equity curve a judge sees is drawn
-- straight from this, so it must start at the account's real opening balance.
CREATE TABLE IF NOT EXISTS equity_curve (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    equity          numeric,
    cash            numeric,
    buying_power    numeric,
    positions_count integer     NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS equity_curve_ts_idx ON equity_curve (ts DESC);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id      bigserial PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    payload jsonb       NOT NULL
);

CREATE INDEX IF NOT EXISTS market_snapshots_ts_idx ON market_snapshots (ts DESC);

CREATE TABLE IF NOT EXISTS candidates (
    id      bigserial PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    symbol  text        NOT NULL,
    reason  text,
    score   numeric,
    payload jsonb
);

CREATE INDEX IF NOT EXISTS candidates_ts_idx ON candidates (ts DESC);
CREATE INDEX IF NOT EXISTS candidates_symbol_ts_idx ON candidates (symbol, ts DESC);

-- Single-row table. The env flag DRY_RUN/KILL_SWITCH halts at the process
-- level; this one can be flipped at runtime from the dashboard without a
-- redeploy, which is what makes it useful during a live demo.
CREATE TABLE IF NOT EXISTS kill_switch (
    id         smallint PRIMARY KEY DEFAULT 1,
    engaged    boolean     NOT NULL DEFAULT false,
    reason     text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kill_switch_singleton CHECK (id = 1)
);

INSERT INTO kill_switch (id, engaged) VALUES (1, false)
    ON CONFLICT (id) DO NOTHING;

-- Per-symbol implied-vol snapshots written by EvidenceCollector via
-- MarketPulseAgent. iv_atm is a vol fraction (0.24 == 24%), kept numeric to
-- stay off float.
CREATE TABLE IF NOT EXISTS iv_history (
    id                  bigserial   PRIMARY KEY,
    ts                  timestamptz NOT NULL DEFAULT now(),
    symbol              text        NOT NULL,
    iv_atm              numeric,
    dte                 integer,
    spot                numeric,
    payload             jsonb,
    put_call_skew       numeric,
    term_structure      numeric,
    median_spread_pct   numeric,
    total_open_interest bigint
);

CREATE INDEX IF NOT EXISTS iv_history_symbol_ts_idx ON iv_history (symbol, ts DESC);

-- One row per StrategyIntent considered. llm_read and matrix_verdict are
-- written by StrategistAgent (Phase 3); arguments/verdict/plan are written by
-- ExecutionAgent; they live in separate columns so partial updates from
-- different agents don't collide.
CREATE TABLE IF NOT EXISTS proposals (
    id             bigserial   PRIMARY KEY,
    ts             timestamptz NOT NULL DEFAULT now(),
    underlying     text        NOT NULL,
    status         text        NOT NULL DEFAULT 'pending',
    intent         jsonb       NOT NULL DEFAULT '{}',
    evidence       jsonb,
    arguments      jsonb,
    verdict        jsonb,
    plan           jsonb,
    error          text
);

-- Phase 3 additions: LLM regime read + deterministic matrix verdict.
ALTER TABLE proposals ADD COLUMN IF NOT EXISTS llm_read jsonb;
ALTER TABLE proposals ADD COLUMN IF NOT EXISTS matrix_verdict jsonb;

CREATE INDEX IF NOT EXISTS proposals_status_ts_idx ON proposals (status, ts);
CREATE INDEX IF NOT EXISTS proposals_underlying_ts_idx ON proposals (underlying, ts DESC);

-- One row per order attempt. client_order_id is the idempotency key end to
-- end: re-running the same proposal must never insert a second row here.
CREATE TABLE IF NOT EXISTS orders (
    id               bigserial PRIMARY KEY,
    proposal_id      bigint      NOT NULL REFERENCES proposals (id),
    client_order_id  text        NOT NULL UNIQUE,
    submitted_at     timestamptz NOT NULL DEFAULT now(),
    status           text        NOT NULL,
    request          jsonb       NOT NULL,
    response         jsonb,
    filled_qty       numeric,
    filled_avg_price numeric,
    error            text
);

CREATE INDEX IF NOT EXISTS orders_proposal_idx ON orders (proposal_id);
CREATE INDEX IF NOT EXISTS orders_status_idx ON orders (status);

-- Every rejection, from either strategy_builder or risk.py.
CREATE TABLE IF NOT EXISTS risk_events (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    proposal_id bigint      REFERENCES proposals (id),
    rule        text        NOT NULL,
    detail      jsonb
);

CREATE INDEX IF NOT EXISTS risk_events_ts_idx ON risk_events (ts DESC);
CREATE INDEX IF NOT EXISTS risk_events_proposal_idx ON risk_events (proposal_id);

-- Local cache of Alpaca's trading calendar. Populated by MarketPulseAgent from
-- get_calendar for a rolling forward window and refreshed once the window
-- shrinks under the configured margin. Every other agent's "is the market open"
-- check reads this table instead of calling get_clock.
CREATE TABLE IF NOT EXISTS market_calendar (
    date          date        PRIMARY KEY,
    open          timestamptz NOT NULL,
    close         timestamptz NOT NULL,
    session_type  text        NOT NULL DEFAULT 'full'
);

-- Single-row cache of account state, upserted by MarketPulseAgent on every
-- tick. Other agents read this instead of calling get_account_info themselves.
CREATE TABLE IF NOT EXISTS account (
    id                     smallint    PRIMARY KEY DEFAULT 1,
    equity                 numeric,
    cash                   numeric,
    buying_power           numeric,
    options_trading_level  int,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT account_singleton CHECK (id = 1)
);

-- Current-state cache of open positions. Sole writer: PositionManagerAgent,
-- piggybacked on its per-tick get_all_positions call. Overwritten in place
-- unlike the append-only tables above.
CREATE TABLE IF NOT EXISTS positions (
    symbol      text        PRIMARY KEY,
    payload     jsonb       NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Per-symbol evidence cache. Sole writer: MarketPulseAgent (every 60s, one row
-- per universe symbol, overwritten in place). StrategistAgent reads from here
-- instead of calling evidence.collect() itself.
CREATE TABLE IF NOT EXISTS evidence (
    symbol      text        PRIMARY KEY,
    payload     jsonb       NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Post-trade lessons written by ReflectionAgent. Keyed on reflected_on so each
-- order or proposal is reflected on at most once.
CREATE TABLE IF NOT EXISTS lessons (
    id            bigserial   PRIMARY KEY,
    ts            timestamptz NOT NULL DEFAULT now(),
    symbol        text,
    lesson        text        NOT NULL,
    source        text        NOT NULL,
    reflected_on  text        NOT NULL
);

CREATE INDEX IF NOT EXISTS lessons_symbol_ts_idx ON lessons (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS lessons_ts_idx ON lessons (ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS lessons_reflected_on_idx ON lessons (reflected_on);

-- LLM call log for token-budget tracking and the decision-timeline dashboard.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                bigserial   PRIMARY KEY,
    ts                timestamptz NOT NULL DEFAULT now(),
    agent             text        NOT NULL,
    model             text        NOT NULL,
    prompt_tokens     integer     NOT NULL DEFAULT 0,
    completion_tokens integer     NOT NULL DEFAULT 0,
    latency_ms        integer     NOT NULL DEFAULT 0,
    ok                boolean     NOT NULL,
    error             text
);

CREATE INDEX IF NOT EXISTS llm_calls_ts_idx ON llm_calls (ts DESC);
CREATE INDEX IF NOT EXISTS llm_calls_agent_ts_idx ON llm_calls (agent, ts DESC);
