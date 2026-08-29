# Phase 1 — Foundation: MCP client, persistence, first live agent

**Master doc:** `00-MASTER.md`
**Repo:** `C:\Users\İsa\OneDrive\Desktop\alpaca\options-m`
**Goal of this phase:** the service boots with a real Alpaca MCP session, writes real
account/market data into Postgres every minute, and is already deployed on Render. No LLM,
no orders yet.

**Local reference repos (read-only — never modify):** `../alpaca-mcp-server` (official Alpaca
MCP server **v2.3.0**) and `../alpaca-skills` (official Alpaca agent skills) are checked out
next to `options-m`. They are the **source of truth** for tool names, parameter schemas,
toolsets and MCP wiring — read them instead of guessing or trusting web docs / memory. Key
files: `alpaca-mcp-server/src/alpaca_mcp_server/tool_registry.py` (exact tool names),
`toolsets.py` (valid `ALPACA_TOOLSETS` values), `overrides.py` (`place_stock_order` /
`place_option_order` / `place_crypto_order` schemas — these are hand-written overrides and
are **not** in `tool_registry.py`), `cli.py` (entrypoint), plus
`alpaca-skills/skills/trading-api/paper-trading-mcp/{SKILL.md,reference.md}` (the official
MCP paper-trading workflow and its guardrails).

---

## Context you need (do not re-explore)

`options-m` is a long-running asyncio service skeleton. Read these five files first — they
are short and you must not break their invariants:

- `src/options_m/__main__.py` (59 lines) — `run()` opens `Database` as an async context
  manager, builds agents and the server, then runs `serve()` and `run_agents()` in one
  `asyncio.TaskGroup`, sharing a single `asyncio.Event` for shutdown.
- `src/options_m/agents.py` (115 lines) — the `Agent` Protocol (`name` property + `async
  step()`), `build_agents(settings, db)`, and `run_agent()` which owns looping, pacing and
  exponential backoff. **An agent's `step()` is one iteration; raising is safe and expected.**
- `src/options_m/config.py` (52 lines) — pydantic-settings `Settings`, `.env`-backed,
  `extra="ignore"`, every field validated with `Field(...)`.
- `src/options_m/db.py` (120 lines) — `Database` wraps `psycopg_pool.AsyncConnectionPool`,
  tolerates an unset `DATABASE_URL` (`is_enabled`), exposes `connection()` and a bounded
  `ping()`.
- `src/options_m/api.py` (77 lines) — `/health` (never touches dependencies), `/ready`
  (checks Postgres), `/` (placeholder dashboard). `create_app(db, agents)` attaches
  dependencies to `app.state`.

Quality gates that must stay green: `ruff check .`, `mypy` (strict, `files = ["src","tests"]`),
`pytest`. Line length 100. Ruff selects `S` (bandit) — no `assert` outside tests, no
`subprocess` without justification.

---

## Deliverables

### 1. Dependencies (`pyproject.toml`)

Move `httpx` from `dev` to `[project].dependencies`, and add:

```
"fastmcp>=3.1",
"alpaca-mcp-server>=2.3",
"httpx>=0.27",
```

`alpaca-mcp-server` 2.3.0 requires Python ≥3.10 (we are on 3.12) and pulls
`fastmcp`, `httpx`, `python-dotenv`, `click`. Its console script is
`alpaca-mcp-server = "alpaca_mcp_server.cli:main"`.

### 2. `src/options_m/config.py` — new settings

Add, keeping the existing `Field(...)` style and grouping comments:

```python
# Alpaca (via the official MCP server)
alpaca_api_key: str | None = None
alpaca_secret_key: str | None = None
alpaca_paper_trade: bool = True
alpaca_toolsets: str = "account,agents,assets,options-data,stock-data,news"
mcp_call_timeout_seconds: float = Field(default=30.0, gt=0)
mcp_max_retries: int = Field(default=2, ge=0)

# Trading universe and safety
universe: str = "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMD,TSLA,META,GOOGL"
dry_run: bool = True          # no write tools reach Alpaca while true
kill_switch: bool = False     # env-level override; DB flag is checked too

# Per-agent cadence (the generic agent_interval_seconds stays as the default)
market_pulse_interval_seconds: float = Field(default=60.0, gt=0)
```

Add a `universe_symbols` property returning a de-duplicated uppercase tuple.

**Do not** put secrets in `.env.example` — only names with empty values.

### 3. `src/options_m/mcp_client.py` — the `AlpacaMcp` facade

The single module in the codebase that talks to Alpaca. Everything else calls typed methods
on it. (`AlpacaTradingAgent` does the same with `alpaca_utils.py` and it works well.)

**Transport:** spawn the MCP server as a stdio subprocess via FastMCP. All of the following
was verified directly against the local checkout (`../alpaca-mcp-server`, v2.3.0) and the
installed `fastmcp` 3.4.x — do not re-derive it:

1. **`python -m alpaca_mcp_server.cli` does not work.** `cli.py` defines a `click` command
   named `main` but has **no `if __name__ == "__main__":` guard** and the package ships no
   `__main__.py`, so `-m` imports the module, exits 0 and leaves us on a dead pipe.
2. **The FastMCP 3.x import path is `fastmcp.client`**, not `fastmcp.client.transports`.
   `fastmcp` is now a meta-package pulling `fastmcp-slim[client,server]`.
3. **`alpaca_mcp_server` ships no `py.typed`.** Never import it from `src/` — subprocess
   only. That keeps strict mypy clean and preserves the "one Alpaca touchpoint" rule.
4. **`cli.py` exits 1 when `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` are missing**, so
   `AlpacaMcp` must not even attempt a spawn without keys — it reports `is_enabled == False`,
   exactly like `Database` without `DATABASE_URL`.
5. The CLI has **no subcommands** (a leading `serve` arg is stripped for back-compat). Flags:
   `--transport {stdio,streamable-http,sse}` (stdio default), `--host`, `--port`,
   `--env-file`. **Never pass `--env-file`** — credentials go in via the subprocess `env`
   only, so nothing touches disk.

```python
import os, sys
from fastmcp.client import Client, StdioTransport

transport = StdioTransport(
    command=sys.executable,
    args=["-c", "from alpaca_mcp_server.cli import main; main()"],
    env={
        **os.environ,                     # MERGE, never replace — see below
        "ALPACA_API_KEY": settings.alpaca_api_key or "",
        "ALPACA_SECRET_KEY": settings.alpaca_secret_key or "",
        "ALPACA_PAPER_TRADE": "true" if settings.alpaca_paper_trade else "false",
        "ALPACA_TOOLSETS": settings.alpaca_toolsets,
    },
)
```

`sys.executable -c` needs no PATH resolution and behaves identically on Windows and in the
container. `shutil.which("alpaca-mcp-server")` (the console script; `/opt/venv/bin` in our
image) stays a documented fallback only.

**Merge the child env over `os.environ`, never replace it** — a bare env dict breaks
subprocess spawn on Windows (`SystemRoot`) and strips `PATH` inside the image.

`ALPACA_TOOLSETS` is the server-side allowlist. Valid values (from `toolsets.py`): `account`,
`trading`, `watchlists`, `assets`, `stock-data`, `crypto-data`, `options-data`,
`corporate-actions`, `news`, `fixed-income-data`, `locates`. Ours:
`account,trading,assets,stock-data,options-data,news` — crypto, watchlists, locates and fixed
income are dead weight in the tool list and only invite wrong tool calls.

On first connect, call `list_tools()` once and log the names: cheap, and it catches a toolset
misconfiguration at boot instead of mid-session.

**Required behaviour:**

- `async with` lifecycle (`__aenter__` / `__aexit__`) mirroring `Database`, so
  `__main__.run()` can nest it. Connect lazily; a missing API key must log a warning and
  leave `is_enabled == False` rather than crash the process (same contract as `Database`).
- One long-lived session. **Do not construct a client per call** —
  `AlpacaTradingAgent` builds a new `TradingClient` on every call and pays for it.
- `async def call(self, tool: str, args: dict[str, Any]) -> Any` — applies
  `asyncio.timeout(mcp_call_timeout_seconds)`, retries `mcp_max_retries` times on transport
  errors with a short backoff, and reconnects the session on a broken pipe.
- **Write-tool guard:** a module-level frozenset `WRITE_TOOLS = {"place_stock_order",
  "place_crypto_order", "place_option_order", "close_position", "close_all_positions",
  "cancel_order_by_id", "cancel_all_orders", "replace_order_by_id",
  "exercise_options_position", "do_not_exercise_options_position", "update_account_config",
  plus the watchlist mutators (`create_watchlist`, `update_watchlist_by_id`,
  `delete_watchlist_by_id`, `add_asset_to_watchlist_by_id`,
  `remove_asset_from_watchlist_by_id`)}`. The guard is enforced **inside `call()`**, not at
  call sites. When `settings.dry_run` is true, calling one raises
  `DryRunViolation` — it never reaches Alpaca.
- Typed convenience methods used later; add them as phases need them. Phase 1 needs:
  `get_clock()`, `get_account_info()`, `get_account_config()` (it carries the options
  trading level), `get_all_positions()`, `get_market_movers()`, `get_most_active_stocks()`,
  `get_news(symbols, limit)`.
- Parse results defensively: FastMCP tools return content blocks whose text is JSON. Write
  one `_as_json(result)` helper and use it everywhere. **On a parse failure raise** — never
  substitute a default. (`VibeHedge` returns a fake $100k account on failure; that class of
  bug is what makes a demo lie to its judges.)
- **Unwrap the trust-boundary envelope.** Verified in
  `../alpaca-mcp-server/src/alpaca_mcp_server/security.py`: v2 runs a `TrustBoundaryMiddleware`
  that wraps *every* tool result, so `structured_content` is always

  ```json
  {"_alpaca_mcp_security": {"trust": "untrusted_tool_output", "tool_name": "...",
                            "risk": "api_structured|external_text", "instructions": "..."},
   "data": { ...the actual Alpaca payload... }}
  ```

  `_as_json()` must prefer `result.structured_content`, and when the dict contains
  `_alpaca_mcp_security`, return `payload["data"]` — otherwise every field lookup in every
  agent is off by one level. Keep the `risk` value: `external_text` (news and similar) is
  what Phase 3 must fence off inside prompts. Fall back to joining text content blocks only
  when `structured_content` is `None`; `../alpaca-mcp-server/tests/test_paper_integration.py`
  (`_parse`/`_to_dict`, lines 36-70) is a working reference implementation of exactly this.
- No numeric field is ever coerced to 0.0 on failure. Copy `_finite_float` semantics from
  `AlpacaTradingAgent/tradingagents/safety/guardrails.py:55-68`: NaN or a non-numeric
  reads as `None` ("unavailable"), never as a passing value.

**Available tool names** — authoritative list, extracted from
`../alpaca-mcp-server/src/alpaca_mcp_server/tool_registry.py` and `overrides.py` at v2.3.0.
Re-grep it if anything looks off; never invent a tool name.

Toolsets we enable:

- `account`: `get_account_info`, `get_account_config`, `update_account_config`,
  `get_portfolio_history`, `get_account_activities`, `get_account_activities_by_type`
- `trading`: `get_orders`, `get_order_by_id`, **`get_order_by_client_id`**,
  `replace_order_by_id`, `cancel_order_by_id`, `cancel_all_orders`, `get_all_positions`,
  `get_open_position`, `close_position`, `close_all_positions`,
  `exercise_options_position`, `do_not_exercise_options_position`
- `assets`: `get_all_assets`, `get_asset`, `get_option_contracts`, `get_option_contract`,
  `get_calendar`, `get_clock`, `get_corporate_action_announcements`,
  `get_corporate_action_announcement`
- `stock-data`: `get_stock_bars`, `get_stock_quotes`, `get_stock_trades`,
  `get_stock_latest_bar`, `get_stock_latest_quote`, `get_stock_latest_trade`,
  `get_stock_snapshot`, `get_most_active_stocks`, `get_market_movers`
- `options-data`: `get_option_bars`, `get_option_trades`, `get_option_latest_trade`,
  `get_option_latest_quote`, `get_option_snapshot`, `get_option_chain`,
  `get_option_exchange_codes`
- `news`: `get_news`
- **Order placement lives in `overrides.py`, not the registry** (the raw `postOrder`
  operation is deliberately excluded): `place_stock_order`, `place_crypto_order`,
  `place_option_order`.

Three consequences for later phases: `get_order_by_client_id` gives us idempotent
reconciliation straight from our own `client_order_id` with no local id mapping;
`get_account_config` exposes the **options trading level**, so the Level-2-vs-Level-3
question is answered at boot instead of by hand in Phase 5; and
`do_not_exercise_options_position` exists, so an expiring long option can be handled
explicitly instead of being left to auto-exercise.

### 4. `src/options_m/schema.sql` + `src/options_m/migrate.py`

Idempotent DDL (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`). Tables to
create now — later phases only add columns/tables:

- `agent_runs(id, agent, started_at, duration_ms, ok, error, detail jsonb)`
- `equity_curve(id, ts, equity numeric, cash numeric, buying_power numeric, positions_count int)`
- `market_snapshots(id, ts, payload jsonb)`
- `candidates(id, ts, symbol, reason, score numeric, payload jsonb)`
- `kill_switch(id smallint primary key default 1, engaged bool not null default false, reason text, updated_at timestamptz)`

Timestamps are `timestamptz default now()`. Money is `numeric`, never `float`.

`migrate.py` exposes `async def apply(db: Database) -> None` that reads `schema.sql` (via
`importlib.resources`, so it works from the wheel) and executes it in one transaction.
No-op when `db.is_enabled` is false. Called once from `__main__.run()` after the pool opens.

### 5. `src/options_m/store.py` — repository layer

A `Store` class wrapping `Database`. All SQL in the codebase lives here.

- `__init__(self, db: Database)`.
- When `db.is_enabled` is false, fall back to bounded in-memory `deque`s so local dev and
  tests work with no Postgres. Log this once at startup, clearly.
- Phase-1 methods: `record_agent_run(...)`, `append_equity(...)`,
  `save_market_snapshot(...)`, `save_candidates(...)`, `recent_agent_runs(limit)`,
  `recent_equity(limit)`, `is_kill_switch_engaged()`, `set_kill_switch(engaged, reason)`.
- Batch writes. Neon's free tier gives 100 compute-hours/month and every touch keeps the
  compute awake — the README already warns about this. One insert per agent iteration is
  fine; per-symbol inserts inside a loop are not.

### 6. `../../src/options_m/agents/market_pulse.py` — first real agent

Implements the `Agent` protocol.

```
step():
  1. clock = await mcp.get_clock()            -> is_open, next_open, next_close
  2. account = await mcp.get_account_info()   -> equity, cash, buying_power
  3. positions = await mcp.get_all_positions()
  4. store.append_equity(...)
  5. if clock.is_open:
         movers  = await mcp.get_market_movers()
         actives = await mcp.get_most_active_stocks()
         news    = await mcp.get_news(universe, limit=...)
         score candidates deterministically (|% change|, relative volume, news count),
         intersected with settings.universe_symbols
         store.save_candidates(...)
     else:
         log and return early — do not burn API calls or Neon compute while closed
```

Market state comes **only** from `get_clock`. Never hardcode a calendar.

> **Superseded 2026-08-29 — see the Addendum at the end of this doc.** This describes
> `MarketPulseAgent` as shipped in Phase 1. The revised design (technical-analysis-only,
> local caching) has `MarketPulseAgent` populate a `market_calendar` table from `get_calendar`
> once at startup instead of calling `get_clock` every tick, and add an `account` table
> alongside `equity_curve`. `get_news` is removed. The addendum has the exact follow-up.

### 7. Wiring

- `agents.py`: delete `HeartbeatAgent`; `build_agents(settings, mcp, store)` returns
  `[MarketPulseAgent(...)]` for now. Keep `run_agent` / `run_agents` untouched.
- `run_agent` currently paces every agent with the single global
  `settings.agent_interval_seconds`. Add an optional `interval_seconds` property to the
  `Agent` protocol (defaulting to the global setting) so each agent can set its own cadence —
  this is a small, contained change to `run_agent`'s `delay` computation.
- `__main__.py`: nest the lifecycles —
  `async with Database(settings) as db, AlpacaMcp(settings) as mcp:` → `await migrate.apply(db)`
  → `store = Store(db)` → `build_agents(settings, mcp, store)`.
- `api.py`: add `/api/status` returning `{clock, account, agents, equity_tail}` from the
  store, and `/api/agent-runs`. Leave `/health` and `/ready` exactly as they are.

### 8. Deploy early

- `.env.example`: add every new variable with an empty value plus a one-line comment.
- `render.yaml`: add `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (both `sync: false`),
  `ALPACA_PAPER_TRADE=true`, `DRY_RUN=true`, `ALPACA_TOOLSETS`.
- Create the Neon database, apply the Render blueprint, set the secrets in the dashboard.
- Set up an UptimeRobot monitor hitting `https://<service>.onrender.com/health` every 5
  minutes — without it Render sleeps the service after 15 idle minutes and the agent loops
  stop silently. The README already documents why.
- Confirm the Docker image can spawn the MCP subprocess (the venv is at `/opt/venv`, and the
  container runs as the unprivileged `app` user).

---

## Tests (`tests/`)

Follow the existing style: `pytest-asyncio` in `auto` mode, `httpx` client for the API.

- `test_mcp_client.py` — a fake in-memory FastMCP server (FastMCP supports passing a server
  object straight to `Client`, which makes this easy): tool call success, timeout, retry,
  reconnect, `DryRunViolation` on every write tool, `_as_json` raising on garbage.
- `test_store.py` — in-memory fallback behaviour; kill-switch round-trip.
- `test_market_pulse.py` — with a fake MCP: market closed → early return, no candidate
  write; market open → equity + candidates written; MCP error propagates out of `step()`
  (so the supervisor's backoff handles it) rather than being swallowed.
- Keep `test_agents.py` passing after the `interval_seconds` change.

---

## Acceptance criteria

- [x] `ruff check . && mypy && pytest` all green (56 tests).
- [ ] `python -m options_m` with real paper keys logs a live clock and account equity, and
      writes rows to `equity_curve` and `agent_runs`. **(needs paper-account keys)**
- [ ] `curl localhost:8080/api/status` returns live account data. **(needs paper-account
      keys; verified returning honest nulls without them)**
- [x] With `ALPACA_API_KEY` unset the process still boots and `/ready` reports the MCP
      session as unavailable — no crash loop.
- [x] `DRY_RUN=true` makes any write-tool call raise `DryRunViolation` (asserted over the
      whole `WRITE_TOOLS` set, and every entry confirmed to be a real server tool).
- [ ] Deployed on Render, `/health` green, UptimeRobot pinging it. **(needs account access)**

---

## Paper-mode enforcement (added after reviewing `../alpaca-skills`)

Alpaca's own `alpaca-trading-paper-trading-mcp` skill addresses our exact case and it
changed this phase. Its §4 "Standalone automation" says the skill's interactive paper gate
does **not** cover unattended automation, and that such a service:

> "must assert paper itself, at startup, and exit if it cannot — construct the client with
> `paper=True` as a literal rather than reading the endpoint from configuration, and abort if
> a live endpoint or live-trading flag is present in the environment. A live account returns
> the same response shape as a paper one, so nothing later in the run will surface the error."

What we implemented, and why each piece is load-bearing:

1. **Paper is pinned as a literal, not read from configuration.** `AlpacaMcp._build_transport`
   always sends `ALPACA_PAPER_TRADE="true"` to the child. Verified from the server source
   (`server.py:117`) that this is the **only** switch for the trading endpoint — there is no
   base-URL override for trading, only `DATA_API_URL` for read-only market data. Pinning it
   therefore makes the live endpoint *unreachable*, not merely unused.
2. **Startup aborts on any live-selecting environment.** `assert_paper_intent()` runs at the
   very top of `main()`, *before* `Settings()` is constructed. The server's check is
   `os.environ.get("ALPACA_PAPER_TRADE", "true").lower() in ("true", "1", "yes")` — with **no
   `.strip()`** — so `"true "`, `"paper"` and `"yes!"` all select live. Absent passes (the
   server defaults to paper). Running the gate before pydantic matters: `"true "` otherwise
   surfaces as an opaque `bool_parsing` error instead of the actual danger. Verified: all of
   `false`, `true `, `paper`, `0` exit 1 with an actionable message.
3. **Unproven reads as live.** After connecting, we corroborate via `get_account_info`
   (`account_number` starting `PA`, or `status == "PAPER_ONLY"`). The skill is explicit that
   neither is a documented guarantee, so this can only *support* the assertion — never replace
   it. A failed read leaves the flag `None` (unknown), and **write tools are refused while it
   is anything but `True`**. Reads stay available, so an outage makes the service blind, not
   reckless.
4. **Unscoped and irreversible tools are permanently disabled** — `FORBIDDEN_TOOLS` =
   `cancel_all_orders`, `close_all_positions`, `exercise_options_position`,
   `do_not_exercise_options_position`. The skill's rule 9 requires explicit human confirmation
   for each "regardless of `confirmation_mode`"; an unattended service has nobody to ask, so
   the only correct answer is no. Scoped `close_position` stays available — it is how
   positions close.
5. **Capture `options_trading_level`, not `options_approved_level`.** The former is the
   effective level (the minimum of the approved level and the account's configured maximum),
   and per the skill it is the one to gate on. Now read at connect and exposed on
   `/api/status`, which removes a manual step from Phase 5.

### Two bugs this work surfaced

- **Boot deadlock.** Corroboration was running inside the connect lock. It issues a tool call,
  and a failing tool call reconnects, which needs the same lock — and `asyncio.Lock` is not
  reentrant. With bad keys the process hung at startup instead of degrading. Corroboration now
  runs outside the lock. Regression test: `test_corroboration_does_not_run_under_the_connect_lock`.
- **Needless respawn on tool errors.** Any exception was treated as a poisoned transport, so a
  plain HTTP 401 tore down and respawned the MCP subprocess. `ToolError` (the server answered;
  the tool failed) is now retried without a reconnect; only transport and timeout failures
  respawn.

---

## Status

**Complete.** `ruff check .`, `mypy` (strict) and `pytest` (86 tests) are green. The service
boots with neither credentials nor a database and degrades honestly; with credentials it
spawns the real MCP server, discovers all 54 tools, and refuses every write tool under
`DRY_RUN`, under an uncorroborated account, and permanently for the unscoped ones. Live
trading is structurally unreachable. Remaining: the operator steps in section 8 (Neon, Render,
UptimeRobot), which need account access.

---

## Traps (learned from the reference projects)

- Never fabricate account or market values on failure — raise or record `None`.
- Do not build a new client per call; hold one session.
- Do not hardcode market hours; `get_clock` is authoritative. (Superseded below — the point
  still holds, it just now means "authoritative source for the cache," not "call it every
  iteration.")
- Do not let a `numeric` money field become a Python `float` in the DB schema.
- `dry_run` must be enforced in the transport layer, not at the call sites, or one forgotten
  call site places a real order.

---

## Addendum (2026-08-29): technical-analysis-only design + local caching

Everything above this line describes Phase 1 exactly as it shipped, and none of it was
wrong for the design in force at the time. Two decisions made afterward change what the
*next* phase builds on top of this foundation — recorded here rather than by rewriting
history above.

### 1. `news` is dropped from the toolset

The system no longer reads news at all — every decision comes from technical indicators on
the underlying plus the option chain's IV vs. realized vol (see `00-MASTER.md`'s Strategy
Matrix section). Concretely:

- `ALPACA_TOOLSETS` changes from `account,trading,assets,stock-data,options-data,news` to
  **`account,trading,assets,stock-data,options-data`**.
- `get_news` is removed from `AlpacaMcp`'s typed convenience methods; nothing calls it.
- **Known drift:** `config.py` shipped in Phase 1 with `alpaca_toolsets: str =
  "account,trading,assets,options-data,stock-data,news"` as the default. That one-line
  default needs to change as part of Phase 2 — flagged here so it is not missed, since
  Phase 1 itself is marked complete and this doc will not be revisited otherwise.

### 2. Market state moves from a live `get_clock` call to a local `market_calendar` cache

The original rule ("market state comes only from `get_clock`, never hardcode a calendar")
was aimed at one specific trap: a hardcoded holiday list that silently drifts out of date
(`AlpacaTradingAgent`'s holiday list hardcoded through 2027). A **cache populated from the
real API and refreshed daily** is not that trap — it is the same authoritative source, just
not re-fetched on every single agent iteration across five agents.

Revised design, to build in Phase 2:

- New table `market_calendar(date primary key, open timestamptz, close timestamptz,
  session_type text)`. `MarketPulseAgent` calls `get_calendar` once at startup for roughly a
  1-year forward window, upserts it, and refreshes once a day (e.g. at its first tick after
  midnight UTC).
- Every other "is the market open right now" check becomes a local read: `now()` between
  today's `open`/`close` in `market_calendar`, no Alpaca round-trip. `risk.py`'s market-hours
  gate reads this table instead of calling `get_clock`.
- `get_clock()` stays on `AlpacaMcp` (it is a real, useful tool) but is no longer called from
  the normal per-iteration agent loops. It would still be reasonable to call it once at
  startup to sanity-check the freshly-loaded calendar row for *today* against Alpaca's live
  clock, but that is optional polish, not a requirement.
- **Accepted risk, stated explicitly:** a once-daily refresh will not catch an unscheduled
  intraday circuit-breaker halt. For a ~4.5-day hackathon run this is an acceptable trade
  against saved API calls and Neon compute; it would need revisiting before running this
  against a longer window or real capital.

### 3. `account` becomes a second table `MarketPulseAgent` owns

`MarketPulseAgent` already calls `get_account_info()`/`get_account_config()` every tick for
`equity_curve`. Add an `account(id smallint primary key default 1, equity numeric, cash
numeric, buying_power numeric, options_trading_level int, updated_at timestamptz)` singleton
row, upserted from that same call — no new Alpaca traffic, since the call was already
happening. `ExecutionAgent`'s buying-power and options-level checks in Phase 2 read this row
locally instead of calling `get_account_info` themselves. Accepted staleness: up to ~60 s
old, which is fine for a paper account; tighten the refresh interval before ever pointing
this at real capital.

Both new tables are specified fully in `phase-2-evidence-risk-execution.md` §2.1 (schema) —
this addendum only records *why* they exist and what part of the already-shipped Phase 1
code they revise.
