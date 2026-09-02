-- Schema for options-m. Applied at startup, idempotently, in one transaction.
--
-- Rules that hold across every table here:
--   * timestamps are timestamptz, defaulting to now()
--   * money is numeric, never double precision — float rounding has no place
--     anywhere near a P/L figure
--   * later phases add tables and columns; nothing here is ever rewritten;
--     new columns on existing tables use ALTER TABLE ... ADD COLUMN IF NOT EXISTS
--
-- Every table carries `account_id text NOT NULL DEFAULT 'default'` so a row can
-- be attributed to the settings it was produced under. The single-state caches
-- (account, kill_switch, positions, evidence) and market_calendar fold it into
-- their primary key; the guarded DO blocks below make that change idempotent
-- against a database created before the column existed.

CREATE TABLE IF NOT EXISTS agent_runs (
    id          bigserial PRIMARY KEY,
    account_id  text        NOT NULL DEFAULT 'default',
    agent       text        NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    duration_ms integer     NOT NULL,
    ok          boolean     NOT NULL,
    error       text,
    detail      jsonb
);
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS agent_runs_agent_started_idx
    ON agent_runs (agent, started_at DESC);
CREATE INDEX IF NOT EXISTS agent_runs_acct_agent_started_idx
    ON agent_runs (account_id, agent, started_at DESC);

-- One row per market-pulse iteration. The equity curve a judge sees is drawn
-- straight from this, so it must start at the account's real opening balance.
CREATE TABLE IF NOT EXISTS equity_curve (
    id              bigserial PRIMARY KEY,
    account_id      text        NOT NULL DEFAULT 'default',
    ts              timestamptz NOT NULL DEFAULT now(),
    equity          numeric,
    cash            numeric,
    buying_power    numeric,
    positions_count integer     NOT NULL DEFAULT 0
);
ALTER TABLE equity_curve ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS equity_curve_ts_idx ON equity_curve (ts DESC);
CREATE INDEX IF NOT EXISTS equity_curve_acct_ts_idx ON equity_curve (account_id, ts DESC);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id         bigserial PRIMARY KEY,
    account_id text        NOT NULL DEFAULT 'default',
    ts         timestamptz NOT NULL DEFAULT now(),
    payload    jsonb       NOT NULL
);
ALTER TABLE market_snapshots ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS market_snapshots_ts_idx ON market_snapshots (ts DESC);
CREATE INDEX IF NOT EXISTS market_snapshots_acct_ts_idx ON market_snapshots (account_id, ts DESC);

CREATE TABLE IF NOT EXISTS candidates (
    id         bigserial PRIMARY KEY,
    account_id text        NOT NULL DEFAULT 'default',
    ts         timestamptz NOT NULL DEFAULT now(),
    symbol     text        NOT NULL,
    reason     text,
    score      numeric,
    payload    jsonb
);
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS candidates_ts_idx ON candidates (ts DESC);
CREATE INDEX IF NOT EXISTS candidates_acct_ts_idx ON candidates (account_id, ts DESC);
CREATE INDEX IF NOT EXISTS candidates_symbol_ts_idx ON candidates (symbol, ts DESC);

-- Runtime halt flag, keyed by account_id. The env flag DRY_RUN/KILL_SWITCH
-- halts the whole process; this row can be flipped at runtime from the
-- dashboard without a redeploy.
CREATE TABLE IF NOT EXISTS kill_switch (
    account_id text        PRIMARY KEY DEFAULT 'default',
    engaged    boolean     NOT NULL DEFAULT false,
    reason     text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE kill_switch ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';
ALTER TABLE kill_switch DROP CONSTRAINT IF EXISTS kill_switch_singleton;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'kill_switch'::regclass AND contype = 'p'
          AND pg_get_constraintdef(oid) = 'PRIMARY KEY (account_id)'
    ) THEN
        IF EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'kill_switch'::regclass AND contype = 'p') THEN
            EXECUTE 'ALTER TABLE kill_switch DROP CONSTRAINT ' ||
                (SELECT conname FROM pg_constraint
                 WHERE conrelid = 'kill_switch'::regclass AND contype = 'p');
        END IF;
        ALTER TABLE kill_switch ADD PRIMARY KEY (account_id);
    END IF;
END $$;
ALTER TABLE kill_switch DROP COLUMN IF EXISTS id;

INSERT INTO kill_switch (account_id, engaged) VALUES ('default', false)
    ON CONFLICT (account_id) DO NOTHING;

-- Per-symbol implied-vol snapshots written by EvidenceCollector via
-- MarketPulseAgent. iv_atm is a vol fraction (0.24 == 24%), kept numeric to
-- stay off float.
CREATE TABLE IF NOT EXISTS iv_history (
    id                  bigserial   PRIMARY KEY,
    account_id          text        NOT NULL DEFAULT 'default',
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

-- Every column past iv_atm was added after the table shipped, and
-- CREATE TABLE IF NOT EXISTS does not backfill columns onto a table that
-- already exists: a deployed database created before them kept failing every
-- INSERT with 'column "dte" ... does not exist'. Restated as ALTERs so the
-- file is idempotent against an old table as well as a fresh one.
ALTER TABLE iv_history ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';
ALTER TABLE iv_history ADD COLUMN IF NOT EXISTS dte integer;
ALTER TABLE iv_history ADD COLUMN IF NOT EXISTS spot numeric;
ALTER TABLE iv_history ADD COLUMN IF NOT EXISTS payload jsonb;
ALTER TABLE iv_history ADD COLUMN IF NOT EXISTS put_call_skew numeric;
ALTER TABLE iv_history ADD COLUMN IF NOT EXISTS term_structure numeric;
ALTER TABLE iv_history ADD COLUMN IF NOT EXISTS median_spread_pct numeric;
ALTER TABLE iv_history ADD COLUMN IF NOT EXISTS total_open_interest bigint;

CREATE INDEX IF NOT EXISTS iv_history_symbol_ts_idx ON iv_history (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS iv_history_acct_symbol_ts_idx ON iv_history (account_id, symbol, ts DESC);

-- One row per StrategyIntent considered. llm_read and matrix_verdict are
-- written by StrategistAgent (Phase 3); arguments/verdict/plan are written by
-- ExecutionAgent; they live in separate columns so partial updates from
-- different agents don't collide.
CREATE TABLE IF NOT EXISTS proposals (
    id             bigserial   PRIMARY KEY,
    account_id     text        NOT NULL DEFAULT 'default',
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
ALTER TABLE proposals ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';
ALTER TABLE proposals ADD COLUMN IF NOT EXISTS llm_read jsonb;
ALTER TABLE proposals ADD COLUMN IF NOT EXISTS matrix_verdict jsonb;

CREATE INDEX IF NOT EXISTS proposals_status_ts_idx ON proposals (status, ts);
CREATE INDEX IF NOT EXISTS proposals_underlying_ts_idx ON proposals (underlying, ts DESC);
CREATE INDEX IF NOT EXISTS proposals_acct_status_ts_idx ON proposals (account_id, status, ts);
CREATE INDEX IF NOT EXISTS proposals_acct_ts_idx ON proposals (account_id, ts DESC);

-- One row per order attempt. client_order_id is the idempotency key end to
-- end: re-running the same proposal must never insert a second row here. It is
-- globally unique (proposals.id is one shared sequence), so the UNIQUE stays
-- unscoped.
CREATE TABLE IF NOT EXISTS orders (
    id               bigserial PRIMARY KEY,
    account_id       text        NOT NULL DEFAULT 'default',
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
ALTER TABLE orders ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS orders_proposal_idx ON orders (proposal_id);
CREATE INDEX IF NOT EXISTS orders_status_idx ON orders (status);
CREATE INDEX IF NOT EXISTS orders_acct_submitted_idx ON orders (account_id, submitted_at DESC);

-- Every rejection, from either strategy_builder or risk.py.
CREATE TABLE IF NOT EXISTS risk_events (
    id          bigserial PRIMARY KEY,
    account_id  text        NOT NULL DEFAULT 'default',
    ts          timestamptz NOT NULL DEFAULT now(),
    proposal_id bigint      REFERENCES proposals (id),
    rule        text        NOT NULL,
    detail      jsonb
);
ALTER TABLE risk_events ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS risk_events_ts_idx ON risk_events (ts DESC);
CREATE INDEX IF NOT EXISTS risk_events_proposal_idx ON risk_events (proposal_id);
CREATE INDEX IF NOT EXISTS risk_events_acct_ts_idx ON risk_events (account_id, ts DESC);

-- Local cache of Alpaca's trading calendar. Populated by MarketPulseAgent from
-- get_calendar for a rolling forward window and refreshed once the window
-- shrinks under the configured margin. Every other agent's "is the market open"
-- check reads this table instead of calling get_clock.
CREATE TABLE IF NOT EXISTS market_calendar (
    account_id    text        NOT NULL DEFAULT 'default',
    date          date        NOT NULL,
    open          timestamptz NOT NULL,
    close         timestamptz NOT NULL,
    session_type  text        NOT NULL DEFAULT 'full',
    PRIMARY KEY (account_id, date)
);
ALTER TABLE market_calendar ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'market_calendar'::regclass AND contype = 'p'
          AND pg_get_constraintdef(oid) = 'PRIMARY KEY (account_id, date)'
    ) THEN
        IF EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'market_calendar'::regclass AND contype = 'p') THEN
            EXECUTE 'ALTER TABLE market_calendar DROP CONSTRAINT ' ||
                (SELECT conname FROM pg_constraint
                 WHERE conrelid = 'market_calendar'::regclass AND contype = 'p');
        END IF;
        ALTER TABLE market_calendar ADD PRIMARY KEY (account_id, date);
    END IF;
END $$;

-- Cache of account state, keyed by account_id, upserted by MarketPulseAgent on
-- every tick. Other agents read this instead of calling get_account_info.
CREATE TABLE IF NOT EXISTS account (
    account_id             text        PRIMARY KEY DEFAULT 'default',
    equity                 numeric,
    cash                   numeric,
    buying_power           numeric,
    options_trading_level  int,
    updated_at             timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE account ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';
ALTER TABLE account DROP CONSTRAINT IF EXISTS account_singleton;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'account'::regclass AND contype = 'p'
          AND pg_get_constraintdef(oid) = 'PRIMARY KEY (account_id)'
    ) THEN
        IF EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'account'::regclass AND contype = 'p') THEN
            EXECUTE 'ALTER TABLE account DROP CONSTRAINT ' ||
                (SELECT conname FROM pg_constraint
                 WHERE conrelid = 'account'::regclass AND contype = 'p');
        END IF;
        ALTER TABLE account ADD PRIMARY KEY (account_id);
    END IF;
END $$;
ALTER TABLE account DROP COLUMN IF EXISTS id;

-- Current-state cache of open positions. Sole writer: PositionManagerAgent,
-- piggybacked on its per-tick get_all_positions call. Overwritten in place
-- unlike the append-only tables above.
CREATE TABLE IF NOT EXISTS positions (
    account_id  text        NOT NULL DEFAULT 'default',
    symbol      text        NOT NULL,
    payload     jsonb       NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, symbol)
);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'positions'::regclass AND contype = 'p'
          AND pg_get_constraintdef(oid) = 'PRIMARY KEY (account_id, symbol)'
    ) THEN
        IF EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'positions'::regclass AND contype = 'p') THEN
            EXECUTE 'ALTER TABLE positions DROP CONSTRAINT ' ||
                (SELECT conname FROM pg_constraint
                 WHERE conrelid = 'positions'::regclass AND contype = 'p');
        END IF;
        ALTER TABLE positions ADD PRIMARY KEY (account_id, symbol);
    END IF;
END $$;

-- Per-symbol evidence cache. Sole writer: MarketPulseAgent (every 60s, one row
-- per universe symbol, overwritten in place). StrategistAgent reads from here
-- instead of calling evidence.collect() itself.
CREATE TABLE IF NOT EXISTS evidence (
    account_id  text        NOT NULL DEFAULT 'default',
    symbol      text        NOT NULL,
    payload     jsonb       NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, symbol)
);
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'evidence'::regclass AND contype = 'p'
          AND pg_get_constraintdef(oid) = 'PRIMARY KEY (account_id, symbol)'
    ) THEN
        IF EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'evidence'::regclass AND contype = 'p') THEN
            EXECUTE 'ALTER TABLE evidence DROP CONSTRAINT ' ||
                (SELECT conname FROM pg_constraint
                 WHERE conrelid = 'evidence'::regclass AND contype = 'p');
        END IF;
        ALTER TABLE evidence ADD PRIMARY KEY (account_id, symbol);
    END IF;
END $$;

-- Post-trade lessons written by ReflectionAgent. Keyed on reflected_on so each
-- order or proposal is reflected on at most once.
CREATE TABLE IF NOT EXISTS lessons (
    id            bigserial   PRIMARY KEY,
    account_id    text        NOT NULL DEFAULT 'default',
    ts            timestamptz NOT NULL DEFAULT now(),
    symbol        text,
    lesson        text        NOT NULL,
    source        text        NOT NULL,
    reflected_on  text        NOT NULL
);
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS lessons_symbol_ts_idx ON lessons (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS lessons_ts_idx ON lessons (ts DESC);
CREATE INDEX IF NOT EXISTS lessons_acct_ts_idx ON lessons (account_id, ts DESC);
DROP INDEX IF EXISTS lessons_reflected_on_idx;
CREATE UNIQUE INDEX IF NOT EXISTS lessons_acct_reflected_on_idx
    ON lessons (account_id, reflected_on);

-- LLM call log for token-budget tracking and the decision-timeline dashboard.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                bigserial   PRIMARY KEY,
    account_id        text        NOT NULL DEFAULT 'default',
    ts                timestamptz NOT NULL DEFAULT now(),
    agent             text        NOT NULL,
    model             text        NOT NULL,
    prompt_tokens     integer     NOT NULL DEFAULT 0,
    completion_tokens integer     NOT NULL DEFAULT 0,
    latency_ms        integer     NOT NULL DEFAULT 0,
    ok                boolean     NOT NULL,
    error             text
);
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS account_id text NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS llm_calls_ts_idx ON llm_calls (ts DESC);
CREATE INDEX IF NOT EXISTS llm_calls_agent_ts_idx ON llm_calls (agent, ts DESC);
CREATE INDEX IF NOT EXISTS llm_calls_acct_ts_idx ON llm_calls (account_id, ts DESC);
