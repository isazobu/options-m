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
);

CREATE INDEX IF NOT EXISTS iv_history_symbol_ts_idx ON iv_history (symbol, ts DESC);
