# options-m → Autonomous Options Trading Agent — MASTER PLAN

> This is the **master document**. Each phase has its own self-contained execution doc
> (see [Phase index](#phase-index)). Work through them in order; each one carries enough
> context to be executed in a fresh session without re-reading the others.
>
> These docs live in `options-m/docs/plan/` and are versioned with the code.

---

## Context

**Why:** LabLab.ai × Alpaca × Featherless "AI Trading Agents" hackathon. Deadline
**4 September 2026, 15:00 UTC** (~6 days from 29 Aug). Mandatory rules (verified against
the hackathon page and the requirement matrix in `../VibeHedge/implementation_plan.md`):

1. An autonomous AI trading agent.
2. Must use Alpaca's **Trading API** *and* either its **MCP server** or **CLI**.
3. **Every strategy must involve options trading.**
4. A **brand-new dedicated paper account with $100,000** starting balance.
5. Deliverables: 1-page write-up, demo video, slide deck, submission metadata, 16:9 cover image.
6. Featherless AI integration = sponsor bonus (voucher code `ALPACAA26`).

**Starting point:** `options-m` is currently a business-logic-free but very solid
long-running service skeleton — independent supervised agent loops in a single asyncio
process (`agents.py`, with per-iteration error isolation and exponential backoff), a
FastAPI admin/health surface (`api.py`, `server.py`), a Postgres pool (`db.py`), clean
SIGTERM handling (`lifecycle.py`), env-driven config (`config.py`), plus a multi-stage
Dockerfile and a Render blueprint. 925 lines, strict mypy, ruff, pytest.

**Target outcome:** On top of that skeleton, a **24/7 multi-agent autonomous options
trading service** that consumes the official Alpaca MCP server in-process, reasons with
Featherless-hosted LLMs, and exposes a live judge-facing dashboard. Deployed on Render
with Postgres on Neon.

**Differentiating thesis:** The reference projects are one-shot CLI runs (`TradingAgents`,
`AlpacaTradingAgent`) or a naive `while True` daemon (`VibeHedge`). Ours is a
*supervised, persistent, auditable service*: every autonomous decision stores its evidence
pack, the agents' arguments, the risk-gate verdict, and the order outcome in Postgres, and
the dashboard lets a judge replay any decision after the fact.

### Local reference repos (read-only — never modify)

Two **official Alpaca repos** are checked out next to `options-m` and are the source of truth
for everything MCP-related in these docs. Read them instead of guessing tool names or
trusting web docs / model memory.

| Path | What it is | How we use it |
| --- | --- | --- |
| `../alpaca-mcp-server` | The official Alpaca MCP server, **v2.3.0** — a full FastMCP + OpenAPI rewrite (v1 tool names no longer exist) | This is the package `mcp_client.py` spawns as a stdio subprocess. `src/alpaca_mcp_server/tool_registry.py` = exact tool names; `toolsets.py` = valid `ALPACA_TOOLSETS` values; `overrides.py` = the `place_stock_order` / `place_option_order` / `place_crypto_order` schemas (hand-written, **not** in the registry); `security.py` = the trust-boundary envelope every result is wrapped in; `cli.py` = the entrypoint and its flags; `tests/test_paper_integration.py` = a working reference for parsing results |
| `../alpaca-skills` | Alpaca's official agent skills — one `SKILL.md` + `reference.md` per workflow, for Trading API and Broker API | Development-time guardrails. Most relevant: `skills/trading-api/paper-trading-mcp/` (order preview → paper-mode verification → submit → monitor, over MCP), `skills/trading-api/paper-trading/` (implementation-agnostic version), `skills/trading-api/backtest/` (reproducible backtest workflow + required disclosures). Optional install for the dev session: `npx skills add alpacahq/alpaca-skills --skill alpaca-trading-paper-trading-mcp`, or copy the directory into `~/.claude/skills/` |

Three concrete things they change in the plan (already folded into the phase docs):

1. **Entrypoint** — `python -m alpaca_mcp_server.cli` does *not* work (no `__main__` guard);
   resolve the `alpaca-mcp-server` console script with `shutil.which`. See Phase 1.
2. **Trust-boundary envelope** — every tool result is `{"_alpaca_mcp_security": {...},
   "data": ...}`, so the client must unwrap `data` and keep the `risk` tag. News is tagged
   `external_text` (attacker-influenceable), which is what Phase 3's prompt fence is for.
3. **`place_option_order` contract** — string-typed `qty`/`limit_price`, `time_in_force`
   `"day"` only, ≤4 legs with `ratio_qty`, net debit positive, and errors returned as an
   `{"error": ...}` dict rather than raised. See Phase 2.

The skills also matter for the *submission story*: our two pieces of evidence for hackathon
rule #2 are the in-process official MCP server plus the `options-m` CLI, and the workflow
we implement (preview → paper verification → idempotent submit → monitor) is deliberately
the one Alpaca's own `alpaca-trading-paper-trading-mcp` skill prescribes.

**Not to be confused with** `../AlpacaTradingAgent`, `../TradingAgents` and `../VibeHedge` —
those are third-party *reference projects* we mine for patterns and traps (see the end of
this doc). `alpaca-mcp-server` and `alpaca-skills` are first-party dependencies/standards.

---

## Architecture

### Layer 1 — Supervised loops (existing `Agent` protocol, registered in `build_agents()`)

Each runs at its own cadence, isolated from the others. We deliberately **do not use
LangGraph**: the skeleton already provides a better supervisor for a long-running service,
and adding that dependency in a 6-day window is pure risk.

| Agent | Cadence | LLM | Responsibility |
| --- | --- | --- | --- |
| `MarketPulseAgent` | 60 s | ✗ | MCP `get_clock`, `get_market_movers`, `get_most_active_stocks`, `get_news` → market context + candidate watchlist; writes `equity_curve` |
| `StrategistAgent` | 5–15 min (only while market open) | ✓ | Gather evidence pack → run the reasoning crew → write a typed `StrategyIntent` into `proposals` |
| `ExecutionAgent` | 30 s | ✗ | Pick up `pending` proposals → deterministic risk gates → size → MCP `place_option_order` |
| `PositionManagerAgent` | 60 s | ✗ (optional LLM review) | Open option positions: P/L, DTE, profit-target / stop rules, `close_position` |
| `ReflectionAgent` | hourly / after close | ✓ | Closed trades → extract a lesson → `lessons` table → injected into future prompts |

### Layer 2 — Reasoning crew (inside one `StrategistAgent` iteration)

1. **Evidence pack (deterministic, no LLM):** underlying bars + indicators, option chain
   snapshot (greeks/IV), news, current positions, past lessons. Missing data becomes an
   explicit `NO_DATA_AVAILABLE` sentinel with an instruction forbidding estimation
   (pattern borrowed from `TradingAgents/tradingagents/dataflows/interface.py:242`).
2. **Bull Analyst + Bear Analyst + Volatility Analyst** — run **in parallel** via
   `asyncio.gather`. (Running analysts sequentially is `TradingAgents`' single biggest
   wall-clock mistake.) The volatility analyst reads IV rank / term structure / skew and
   says which *structure* fits.
3. **Portfolio Manager (judge, larger model)** — returns structured JSON:
   `{action, strategy, underlying, direction, target_delta, dte_window, conviction, thesis, invalidation}`.

### Core safety principle: the LLM never invents an option symbol

The PM emits an **intent** only (direction, target delta, DTE window, structure type). A
deterministic `strategy_builder.py` then **selects** real contracts from the live chain
(`get_option_chain` / `get_option_contracts`) closest to the requested delta and DTE, and
assembles the legs. This is the single biggest weakness in all three reference projects —
`VibeHedge` hand-formats OCC symbols and falls back to a hardcoded `550.0` spot price.

**Supported structures (all defined-risk):**
- Long call / long put (Level 2)
- Debit call spread / debit put spread — verticals (Level 3)
- Covered call / cash-secured put (Level 1, when the underlying position exists)
- Naked short legs are hard-rejected in the risk engine.

---

## Repository changes at a glance

### New modules (`src/options_m/`)

| File | Content |
| --- | --- |
| `mcp_client.py` | `AlpacaMcp` facade. `fastmcp.Client` over `StdioTransport` running `alpaca-mcp-server` as a subprocess; long-lived session, reconnect, retry, timeout. **The only module that touches Alpaca.** Refuses write tools in `dry_run` |
| `llm.py` | Featherless client (`httpx` → `POST {base}/chat/completions`). Two tiers: `model_fast` (analysts) / `model_deep` (PM). Pydantic-validated structured output, one repair retry, then **fail-closed** (no trade) |
| `evidence.py` | Evidence-pack collector, compact JSON serialisation, `NO_DATA` sentinels |
| `crew.py` | Bull / Bear / Volatility / PM roles; prompts live as markdown under `prompts/` (configuration, not code) |
| `strategy_builder.py` | `StrategyIntent` → contract selection from the live chain → `OrderPlan` (legs, qty, limit price, max loss) |
| `risk.py` | Deterministic guardrails, zero LLM |
| `store.py` | Postgres repository layer on top of `db.py`; in-memory fallback when the DB is disabled so local dev works |
| `schema.sql` + `migrate.py` | Table schema and an idempotent migration runner |
| `trading/*.py` | One module per Layer-1 agent (`market_pulse.py`, `strategist.py`, `execution.py`, `position_manager.py`, `reflection.py`) |
| `cli.py` | `options-m status \| propose --dry-run \| trade --once \| positions` — cheap, since the logic lives in the modules, and a second piece of evidence for rule #2 |

### Existing files to change

- **`config.py`** — Alpaca (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER_TRADE=true`,
  `ALPACA_TOOLSETS`), Featherless (`FEATHERLESS_API_KEY`,
  `FEATHERLESS_BASE_URL=https://api.featherless.ai/v1`, `FEATHERLESS_MODEL_FAST/DEEP`),
  per-agent intervals, and every risk limit. Keep the existing `Field(...)` validation style.
- **`agents.py`** — drop `HeartbeatAgent`; `build_agents()` constructs the five real agents
  with explicit dependencies (`AlpacaMcp`, `Llm`, `Store`, `RiskEngine`). **Do not touch
  the supervisor logic** — it is already correct.
- **`__main__.py`** — bring `AlpacaMcp` and `Llm` into the `async with` lifecycle next to
  `Database`; run migrations at startup.
- **`api.py`** — dashboard plus JSON API.
- **`.env.example`, `render.yaml`, `Dockerfile`, `pyproject.toml`, `README.md`** — new env
  vars and dependencies (`fastmcp`, `alpaca-mcp-server`; `httpx` moves to prod deps).

### Risk engine (`risk.py`) — hard rules

- Max premium at risk per trade (% of equity) and a total open-premium ceiling
- Max concurrent positions; max positions per underlying
- **Defined risk only** — reject any leg combination with unbounded loss
- DTE window (7–45), minimum open interest / volume, max bid-ask spread %
- Daily-loss halt and drawdown-from-high-water-mark halt
- Kill switch: DB flag + `POST /admin/kill` + env; every agent checks it each iteration
- Market-hours gate **via MCP `get_clock`** — no hardcoded calendars
  (`AlpacaTradingAgent`'s trap #1: holiday lists hardcoded through 2027)
- Idempotency: `client_order_id = f"om-{proposal_id}"`, so one proposal can never place
  two orders (`VibeHedge` re-buys a put every 60 s while a breach persists)
- **Never fake a fill**: a failed order is written as `orders.status='failed'` and never
  reported as success (`VibeHedge` returns `FILLED_SIMULATED` with a hardcoded $3.50 premium)
- Every rejection is written to `risk_events` — strong dashboard material

### Postgres schema

`agent_runs` (per-iteration telemetry), `market_snapshots`, `candidates`, `proposals`
(evidence + each role's argument + PM verdict as JSONB), `orders` (unique
`client_order_id`), `fills`, `positions_history`, `equity_curve`, `risk_events`,
`lessons`, `kill_switch`.

### Dashboard (`api.py` + `static/`)

A single page with no build step (server-rendered HTML + `fetch` polling on three tiers:
2 s / 10 s / 60 s):

- Equity curve + daily P/L, open option positions (greeks, DTE, P/L)
- Live agent status (last iteration, duration, error counter) from `agent_runs`
- **Decision timeline**: expanding a proposal reveals the bull/bear/vol arguments, the
  PM's JSON verdict, the selected contracts and the risk-gate result
- Risk-event feed and a kill-switch button
- `/api/*` JSON endpoints; `/health` and `/ready` stay exactly as they are

---

## Phase index

Work through them in order. Each doc carries enough context to be executed in a fresh session.

| Phase | Doc | Goal | Status |
| --- | --- | --- | --- |
| 1 | `phase-1-foundation.md` | Dependencies, config, `mcp_client.py`, `store.py` + schema, `MarketPulseAgent` running end to end. **Deploy to Render early** + UptimeRobot pinger | ✅ **Code complete** — 86 tests, ruff + strict mypy green. Deployment steps pending (need Neon/Render/Alpaca accounts) |
| 2 | `phase-2-evidence-risk-execution.md` | `evidence.py`, `strategy_builder.py`, `risk.py`, `ExecutionAgent` — producing real order plans from the live chain under `dry_run=true` | ⬜ Next |
| 3 | `phase-3-llm-crew.md` | `llm.py` + `crew.py` (bull/bear/vol/PM), structured output, `StrategistAgent` wired up. First real paper options order | ⬜ |
| 4 | `phase-4-position-reflection-dashboard.md` | `PositionManagerAgent`, `ReflectionAgent`, the dashboard | ⬜ |
| 5 | `phase-5-tests-live-run.md` | Tests, polish, **live paper run** so real trade history accumulates for the judges | ⬜ |
| 6 | `phase-6-submission.md` | Video, write-up, slides, metadata, deploy freeze | ⬜ |

### Blocked on operator action (not code)

These gate Phase 5 and should be done as early as possible:

- [ ] Brand-new Alpaca **paper** account, starting balance exactly $100,000; record the
      Account ID (the submission form asks for it).
- [ ] Neon Postgres database → `DATABASE_URL`.
- [ ] Apply the Render blueprint; set `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `DATABASE_URL`
      as dashboard secrets.
- [ ] UptimeRobot monitor on `/health` every 5 minutes, or Render sleeps the service and the
      agent loops stop silently.
- [ ] Featherless API key (sponsor voucher `ALPACAA26`) → Phase 3.

---

## Verification (whole system)

**Local:**
```bash
pip install -e ".[dev]"
ruff check . && mypy && pytest             # existing quality gates stay green
python -m options_m                         # service + dashboard on :8080
options-m propose --dry-run --symbol SPY    # full decision chain without sending an order
```

**End-to-end (the judge scenario):**
1. Fill `.env` with the fresh paper-account keys and start the service.
2. Watch `MarketPulseAgent` pull candidates and `StrategistAgent` produce a proposal.
3. Open the proposal: bull/bear/vol arguments, PM verdict, and the selected real OCC contracts.
4. `ExecutionAgent` submits → the position appears in the Alpaca paper account and matches
   `orders` by `client_order_id`.
5. Hit the kill switch → agents stop producing new orders, and the event lands in `risk_events`.
6. `PositionManagerAgent` closes a position that hit its profit target →
   `ReflectionAgent` writes the lesson into `lessons`.

---

## Accepted assumptions

- **Options level:** assume up to Level 3 on the paper account (options are enabled by
  default in Alpaca paper). If it turns out to be Level 2, verticals are disabled by env
  flag and we continue with single long calls/puts — `risk.py` keeps this configurable.
- **Featherless models** are not hardcoded; chosen by env — roughly an 8B-class instruct
  model for the fast tier and a 70B-class one for the deep tier.
- **Universe** starts as liquid ETFs + mega caps (SPY, QQQ, IWM, AAPL, MSFT, NVDA, …) so
  option chains have tight spreads.

---

## Alpaca's own guidance (`../alpaca-skills`, `../alpaca-mcp-server`)

Both are official Alpaca repositories checked out beside this one, and they outrank the three
reference projects: the hackathon is judged by Alpaca, and `alpaca-trading-paper-trading-mcp`
describes exactly the system we are building. What it changed:

- **Paper mode is asserted, not configured.** Unattended automation must prove paper at
  startup and exit if it cannot; unproven reads as live, because a live account returns the
  same response shape as a paper one. Implemented in Phase 1 — see that doc's
  "Paper-mode enforcement" section. `ALPACA_PAPER_TRADE` is the server's only trading-endpoint
  switch, so pinning it makes live unreachable.
- **Unscoped and irreversible tools require human confirmation**, which an unattended service
  cannot provide, so `cancel_all_orders`, `close_all_positions`, `exercise_options_position`
  and `do_not_exercise_options_position` are permanently disabled.
- **Gate on `options_trading_level`**, the effective level, never `options_approved_level`.
- **Closing a position is order entry** — market order, market hours, unknown fill price.
  Monitor to a terminal state rather than assuming flat (Phase 4).
- **Build order bodies from the tool schema, never the REST schema** — the `place_*` tools
  reshape the body and set `additionalProperties: false` (Phase 2, §5b).
- **`get_order_by_client_id`** is the documented recovery for an ambiguous submission.

The skills also confirm choices already in this plan: a unique `client_order_id` on every
order, fail-safe on any failed verification, and never inferring a parameter the caller did
not specify.

---

## Know-how harvested from the reference projects

Kept here so it is not lost; each phase doc repeats the parts it needs.

**Steal:**
- Typed intent as the LLM→broker contract; position transition and broker actions derived
  deterministically (`AlpacaTradingAgent/tradingagents/agents/schemas.py:404-466`).
- `strict=True` position reads before execution — a broker outage must never read as "flat"
  (`AlpacaTradingAgent/tradingagents/dataflows/alpaca_utils.py:694-768`).
- A safety layer with zero LLM/agent imports plus a filesystem/DB kill switch
  (`AlpacaTradingAgent/tradingagents/safety/guardrails.py`).
- `_finite_float` — NaN/HTML in a broker numeric field must read as "unavailable", never as
  "breaker passed" (`guardrails.py:55-68`).
- Prompts as overridable markdown files, not inline f-strings
  (`AlpacaTradingAgent/tradingagents/prompts/`).
- Append-only markdown/SQL decision log instead of a vector DB — simpler and human-auditable
  (`TradingAgents/tradingagents/agents/utils/memory.py`).
- Tool-call signature cache + repeat refusal + iteration cap
  (`AlpacaTradingAgent/.../analysts/market_analyst.py:233-278`).
- A `NO_DATA_AVAILABLE: ... Do not estimate or fabricate values` sentinel instead of silent nulls.
- `safe_ticker_component()` — never interpolate an LLM-supplied ticker into a path
  (`TradingAgents/tradingagents/dataflows/utils.py:17-42`).
- Black-Scholes greeks + delta-targeted strike search, Alpaca-decoupled
  (`VibeHedge/src/options/options_lab.py`) — useful as a fallback when the chain snapshot
  lacks greeks.

**Traps to avoid:**
- Silent synthetic/fabricated market data on fetch failure (`VibeHedge/training/download_hourly_data.py:141-145`).
- Silent fake fills (`VibeHedge/src/execution/alpaca_trader.py:196-219`).
- Hardcoded spot-price fallbacks (`VibeHedge/src/mcp_server/server.py:151`).
- `auto_execute=True` as an MCP tool default (`VibeHedge/src/mcp_server/server.py:171`).
- No idempotency / no position awareness in the daemon loop → repeated orders.
- Hardcoded market-hours calendars instead of `get_clock`.
- Constructing a new broker client on every call instead of caching it.
- Pure counter-based debate rounds with no early stop — cost scales with no benefit.
- Signal extraction by scanning the last 100 chars for BUY/SELL — "avoid a SELL here" parses as SELL.
