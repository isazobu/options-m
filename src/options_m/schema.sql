-- Schema for options-m. Applied at startup, idempotently, in one transaction.
--
-- Rules that hold across every table here:
--   * timestamps are timestamptz, defaulting to now()
--   * money is numeric, never double precision — float rounding has no place
--     anywhere near a P/L figure
--   * later phases add tables and columns; nothing here is ever rewritten

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

-- One near-the-money implied-vol reading per underlying per pull. The evidence
-- pack's iv_rank (phase 2) is a symbol's latest reading against its own recent
-- history, so this table has to start filling before the strategist runs.
-- iv_atm is a vol fraction (0.24 == 24%), kept numeric to stay off float.
CREATE TABLE IF NOT EXISTS iv_history (
    id      bigserial   PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    symbol  text        NOT NULL,
    iv_atm  numeric     NOT NULL,
    dte     integer,
    spot    numeric,
    payload jsonb

-- One row per StrategyIntent considered. arguments/verdict stay null until
-- phase 3 writes the bull/bear/vol/PM arguments there.
CREATE TABLE IF NOT EXISTS proposals (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    underlying text        NOT NULL,
    status     text        NOT NULL DEFAULT 'pending',
    intent     jsonb       NOT NULL,
    evidence   jsonb,
    arguments  jsonb,
    verdict    jsonb,
    plan       jsonb,
    error      text
);

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

-- Every rejection, from either strategy_builder or risk.py. "The agent
-- declined 14 trades and why" is stronger judging material than the trades
-- it took. proposal_id is nullable: the kill-switch halt logs one with none.
CREATE TABLE IF NOT EXISTS risk_events (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    proposal_id bigint      REFERENCES proposals (id),
    rule        text        NOT NULL,
    detail      jsonb
);

CREATE INDEX IF NOT EXISTS risk_events_ts_idx ON risk_events (ts DESC);
CREATE INDEX IF NOT EXISTS risk_events_proposal_idx ON risk_events (proposal_id);

-- One row per symbol per evidence-collection run. Feeds iv_rank once a
-- couple of days of history accumulate; started in this phase deliberately.
CREATE TABLE IF NOT EXISTS iv_history (
    id                  bigserial PRIMARY KEY,
    ts                  timestamptz NOT NULL DEFAULT now(),
    symbol              text        NOT NULL,
    iv_atm              numeric,
    put_call_skew       numeric,
    term_structure      numeric,
    median_spread_pct   numeric,
    total_open_interest bigint
);

CREATE INDEX IF NOT EXISTS iv_history_symbol_ts_idx ON iv_history (symbol, ts DESC);

-- Local cache of Alpaca's trading calendar (2026-08-29 design change). Populated
-- by MarketPulseAgent from get_calendar for a rolling forward window and
-- refreshed once the window shrinks under the configured margin. Every other
-- agent's "is the market open" check reads this table instead of calling
-- get_clock -- accepted risk: a once-a-day refresh will not catch an
-- unscheduled intraday circuit-breaker halt.
CREATE TABLE IF NOT EXISTS market_calendar (
    date          date        PRIMARY KEY,
    open          timestamptz NOT NULL,
    close         timestamptz NOT NULL,
    session_type  text        NOT NULL DEFAULT 'full'
);

-- Single-row cache of account state, upserted by MarketPulseAgent on the same
-- per-tick get_account_info/get_account_config call that already feeds
-- equity_curve -- no extra Alpaca traffic. Other agents read this instead of
-- calling get_account_info themselves.
CREATE TABLE IF NOT EXISTS account (
    id                     smallint    PRIMARY KEY DEFAULT 1,
    equity                 numeric,
    cash                   numeric,
    buying_power           numeric,
    options_trading_level  int,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT account_singleton CHECK (id = 1)
);

-- Current-state cache of open positions, keyed by underlying symbol. Sole
-- writer is PositionManagerAgent, piggybacked on its existing per-tick
-- get_all_positions call. Unlike every other table in this file this one is
-- overwritten in place rather than appended to -- it mirrors "what is open
-- right now", not history.
CREATE TABLE IF NOT EXISTS positions (
    symbol      text        PRIMARY KEY,
    payload     jsonb       NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
