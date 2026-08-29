# options-m → Autonomous Options Trading Agent — MASTER PLAN

> This is the **master document**. Each phase has its own self-contained execution doc
> (see [Phase index](#phase-index)). Work through them in order; each one carries enough
> context to be executed in a fresh session without re-reading the others.
>
> These docs live in `options-m/docs/plan/` and are versioned with the code.

---

## Context

**Why:** LabLab.ai × Alpaca × Featherless "AI Trading Agents" hackathon. Deadline
**4 September 2026, 15:00 UTC**. Mandatory rules (verified against the hackathon page and
the requirement matrix in `../VibeHedge/implementation_plan.md`):

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
Dockerfile and a Render blueprint. Phase 1 (below) is code-complete on top of that skeleton.

**Target outcome:** On top of that skeleton, a **24/7 multi-agent autonomous options
trading service** that consumes the official Alpaca MCP server in-process, reasons with a
Featherless-hosted LLM over pure technical analysis (no news), and exposes a live
judge-facing dashboard. Deployed on Render with Postgres on Neon.

**Differentiating thesis:** The reference projects are one-shot CLI runs (`TradingAgents`,
`AlpacaTradingAgent`) or a naive `while True` daemon (`VibeHedge`). Ours is a
*supervised, persistent, auditable service*: every autonomous decision stores its evidence
pack, the LLM's regime read, the deterministic strategy-matrix verdict, the risk-gate
verdict, and the order outcome in Postgres, and the dashboard lets a judge replay any
decision after the fact.

**Operational reality:** this run only has until the 4 Sep 15:00 UTC deadline — see
[Operational window & wind-down](#operational-window--wind-down-45-days) below. That constraint
shapes several design choices (IV/RV over `iv_rank`, the earnings gate, the flatten CLI), so
read it before touching Phase 2 or 3.

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
   "data": ...}`, so the client must unwrap `data` and keep the `risk` tag. This still matters
   even though `news` is no longer in our toolset — any future toolset addition inherits the
   same envelope, and `get_option_chain`/`get_stock_bars` payloads are tagged `api_structured`.
3. **`place_option_order` contract** — string-typed `qty`/`limit_price`, `time_in_force`
   `"day"` only, ≤4 legs with `ratio_qty`, net debit positive/net credit negative, and errors
   returned as an `{"error": ...}` dict rather than raised. See Phase 2.

The skills also matter for the *submission story*: our two pieces of evidence for hackathon
rule #2 are the in-process official MCP server plus the `options-m` CLI, and the workflow
we implement (preview → paper verification → idempotent submit → monitor) is deliberately
the one Alpaca's own `alpaca-trading-paper-trading-mcp` skill prescribes.

**Not to be confused with** `../AlpacaTradingAgent`, `../TradingAgents` and `../VibeHedge` —
those are third-party *reference projects* we mine for patterns and traps (see the end of
this doc). `alpaca-mcp-server` and `alpaca-skills` are first-party dependencies/standards.

---

## Architecture

### Design change (2026-08-29): technical analysis only, no news

The original design ran a Bull/Bear/Volatility analyst crew that read news headlines
alongside price action. That is now replaced end to end: **every decision is driven by
deterministic technical indicators on the underlying plus the option chain's implied vol
relative to realized vol — no news, no sentiment, no headline text anywhere in the evidence
pack or the prompt.** Two consequences ripple through every phase doc:

- `news` is dropped from `ALPACA_TOOLSETS` entirely (Phase 1), `get_news` is never called,
  and the `untrusted_news` / prompt-injection-fence machinery that Phase 3 originally
  specified is gone — there is no external text in the pipeline to fence.
- The three-analyst-plus-judge crew (`crew.py`, `bull_analyst.md`, `bear_analyst.md`,
  `volatility_analyst.md`, `portfolio_manager.md`) is replaced by a **single `StrategistAgent`**
  that internally reads trend + volatility regime with one LLM call and then runs a
  deterministic **Strategy Matrix** to pick the structure. See Phase 3.

### Layer 1 — Supervised loops (existing `Agent` protocol, registered in `build_agents()`)

Each runs at its own cadence, isolated from the others. We deliberately **do not use
LangGraph**: the skeleton already provides a better supervisor for a long-running service,
and adding that dependency this close to the deadline is pure risk.

| Agent | Cadence | LLM | Responsibility |
| --- | --- | --- | --- |
| `MarketPulseAgent` | 60 s | ✗ | MCP `get_clock`/`get_calendar` once at startup → `market_calendar` table; `get_account_info`/`get_account_config` every tick → `account` table; writes `equity_curve` |
| `StrategistAgent` | 5–15 min (only while market open, per local `market_calendar`) | ✓ (one call/iteration) | Filter candidates (position/proposal/earnings-blackout) → evidence pack (technical only) → LLM trend+regime+thesis read → deterministic **Strategy Matrix + Earnings Gate** → typed `StrategyIntent` into `proposals`, or `hold` |
| `ExecutionAgent` | 30 s | ✗ | Pick up `pending` proposals → deterministic risk gates (reading `account`/`positions` locally) → size → MCP `place_option_order` → upsert `orders` |
| `PositionManagerAgent` | 60 s | ✗ | `get_all_positions` → upsert local `positions` cache; P/L, DTE, profit-target / stop rules → `close_position` |
| `ReflectionAgent` | hourly / after close | ✓ | Closed trades (read from local `orders`) → extract a lesson → `lessons` table → injected into the next `StrategistAgent` LLM call for that symbol |

`StrategistAgent` is **one agent, one process, one `step()`** — the trend read, the
volatility-regime read, and the thesis/conviction narrative are internal sub-steps of a
single LLM call, not separate agents or separate LLM calls. See Phase 3 for exactly how
that call is structured and why the matrix decision itself is never delegated to the model.

### Core safety principle: the LLM never invents an option symbol

The LLM's output narrows to a *regime read* (trend direction, IV/RV classification, thesis,
invalidation, conviction). The deterministic Strategy Matrix (Phase 3) turns that regime read
into a `strategy` literal, and `strategy_builder.py` (Phase 2) then **selects real contracts**
from the live chain (`get_option_chain` / `get_option_contracts`) closest to the calibrated
delta and DTE window, and assembles the legs. This is the single biggest weakness in all
three reference projects — `VibeHedge` hand-formats OCC symbols and falls back to a
hardcoded `550.0` spot price.

**Supported structures (all defined-risk, all ≤4 legs):**

| Structure | Legs | Requires |
| --- | --- | --- |
| Long call / long put | 1 | Level 2 fallback when spreads are unavailable |
| Call debit spread / put debit spread | 2 | Level 3 |
| Put credit spread / call credit spread | 2 | Level 3 |
| Long strangle | 2 (both long) | Level 2 |
| Iron condor | 4 | Level 3 |
| Iron butterfly | 4 | Level 3 — only when IV/RV ≥ 1.40 |

Every short leg's protective wing is submitted **in the same multi-leg order** — this is not
just a risk-engine rule, it is an Alpaca API constraint: the MLeg endpoint rejects a
multi-leg order containing a naked short leg, so `risk.py`'s "defined risk only" rule and
Alpaca's own order validation are two independent layers catching the same mistake. See
[Strategy matrix](#strategy-matrix-regime--priced-order) below and Phase 2/3 for the full
mechanics (calibration table, credit/debit-specific checks, sign convention).

---

## Strategy matrix: regime → priced order

Two deterministic reads feed a 2×3 matrix, plus one volatility-intensity override:

- **Trend** (from SMA20/50, ADX, RSI14 on the underlying): yukarı (up) / yatay (flat) / aşağı
  (down).
- **Volatility regime** (chain IV at-the-money ÷ 20-day realized volatility): pahalı
  (expensive, IV/RV ≥ 1.10) / ucuz (cheap, IV/RV < 1.10), with a **çok pahalı** (very
  expensive) tier at IV/RV ≥ 1.40 that upgrades the flat/expensive cell.

| | Prim pahalı (IV/RV ≥ 1.10) | Prim ucuz |
| --- | --- | --- |
| **Yukarı eğilimli** | Put credit spread | Call debit spread |
| **Yatay** | Iron condor (→ **iron butterfly** if IV/RV ≥ 1.40) | Long strangle |
| **Aşağı eğilimli** | Call credit spread | Put debit spread |

The LLM produces the trend/regime *read* (with thesis and invalidation) from the evidence
pack; the matrix lookup and the earnings-blackout gate are pure code — the model never picks
the strategy family or negotiates a threshold. See Phase 3, "Structure gating in code, not
prompt."

**Calibration (measured against a real chain, IV 30% / RV 16%):**

| Structure | Legs | Net | Max loss | Max profit | Example qty |
| --- | --- | --- | --- | --- | --- |
| Iron condor | 4 | +$1.51 | $349 | $151 | 5 |
| Iron butterfly | 4 | +$7.89 | $211 | $789 | 9 |
| Put credit spread | 2 | +$0.74 | $426 | $74 | 4 |
| Call debit spread | 2 | −$1.84 | $184 | $316 | 10 |
| Long strangle | 2 | −$5.27 | $527 | unlimited | 3 |

(Call credit spread and put debit spread on the same chain were not repriced in this pass —
same construction as their mirror structures above; build them the same way and confirm
before relying on the numbers.)

**Short-delta → credit/width calibration**, measured on a 38-day chain (width barely moves
this ratio — short delta does):

| Short delta | Credit / width |
| --- | --- |
| 0.15 | ~10% |
| 0.20 | ~14% |
| 0.25 | ~18% |
| 0.30 | ~21% |
| 0.35 | ~27% |

Minimum acceptable credit/width is **12%** (not 15% — that made the 0.20-delta setup
unreachable). To get fatter credit, raise `SHORT_DELTA`, not the wing width. See Phase 2 for
the code home of this table.

**Credit vs. debit structures check different things.** Credit structures (put/call credit
spread, iron condor, iron butterfly): still-credit-at-worst-fill, credit ≥ 12% of width,
IV > RV edge. Debit structures (call/put debit spread): genuinely debit, paying ≤ 45% of
width, reward/risk ≥ 1×. Long strangle has no width concept — max loss is the premium paid.

**Sign convention**, verified against real prices: Alpaca's multi-leg `limit_price` is
positive = debit, negative = credit. `net_worst` in our own calculation is computed positive
when we are collecting a credit, so `-net_worst` yields the correctly signed value in both
cases (iron condor → −1.51 submitted, debit spread → +1.84 submitted).

**Earnings gate:** `earnings.py` (new module, Phase 2/3) blocks any *new* structure on a
symbol within its earnings blackout window (3 days before through 1 day after, by default) —
selling premium into a print is the most common way a short-vol strategy blows up. Alpaca
exposes no earnings-calendar endpoint, so this is a hand-maintained dict; see
[Operational window & wind-down](#operational-window--wind-down-45-days) for why it is
dormant for this specific run and must not be deleted anyway.

---

## Local cache: what we still fetch live vs. what we read from Postgres

Per an explicit design decision: **do not hit Alpaca for everything.** Four tables move from
"ask Alpaca live" to "read from a local cache that one agent owns and refreshes":

| Table | Sole writer | Refresh | Who reads it locally | Accepted staleness risk |
| --- | --- | --- | --- | --- |
| `market_calendar` | `MarketPulseAgent` | Once at startup (`get_calendar`, ~1yr window), then daily | Every agent's "is the market open" check | Won't catch an unscheduled circuit-breaker halt — accepted for a run this short |
| `account` | `MarketPulseAgent` | Every 60 s tick (piggybacked on the existing `get_account_info`/`get_account_config` call — no new Alpaca traffic) | `ExecutionAgent`'s buying-power / options-level checks | Up to ~60 s stale; fine on paper, would need tightening for real capital |
| `positions` | `PositionManagerAgent` | Every 60 s tick (piggybacked on the existing `get_all_positions` call) | `StrategistAgent`'s "already positioned in this underlying" pre-filter | Up to ~60 s stale |
| `orders` | `ExecutionAgent` | Upserted on submit and on every reconciliation pass | `ReflectionAgent` (never calls `get_orders` live) | None — write-through on every state change |

`get_clock()` is **removed from the normal agent loop** — it was called by nearly every
agent every iteration in the original design, and that is exactly the live-call volume this
change eliminates. `MarketPulseAgent` is now the only agent that calls `get_calendar`, and
every other market-open check becomes a local read against `market_calendar`. See Phase 1
for the schema and Phase 2 for the migration path.

---

## Operational window & wind-down (~4.5 days)

This run only executes from now through **4 September 2026, 15:00 UTC** (11:00 EDT, mid
trading day). That is roughly 4.5 calendar days, or three-and-a-bit full trading sessions.
Three consequences, all already folded into the phase docs:

1. **The earnings gate is coded but dormant for this run.** None of the 7 non-ETF names in
   the fixed universe report earnings before the deadline (nearest is TSLA, ~Oct 21) — see
   `earnings.py`. Do not delete or skip implementing the gate: it is required for
   correctness and for the submission story, it simply will not trigger this week.
2. **IV/RV ratio, not `iv_rank`, is the primary volatility signal.** `iv_rank` needs a
   history of chain snapshots to become meaningful (Phase 2 originally planned to let it
   mature "within a day or two of running"); in a ~4.5-day window it never accumulates enough
   samples to be trustworthy. The IV/RV ratio needs only the current chain snapshot plus a
   20-day realized-vol computation from bars we already have, so it is correct from the very
   first `StrategistAgent` iteration. Keep collecting `iv_history` anyway (it costs nothing
   and is good submission material), but never gate a decision on `iv_rank` in this run.
3. **A wind-down policy is required before the deadline, or the judges inherit open risk.**
   Chosen default (no strong preference expressed): `ExecutionAgent` stops opening *new*
   positions starting **2–3 hours before the 15:00 UTC deadline**, and every still-open
   position is closed deterministically, **one `close_position` call per symbol**, through a
   new, manually-triggered `options-m flatten` CLI subcommand — never an automatic unattended
   timer, and never `close_all_positions` (permanently forbidden regardless). See Phase 4 for
   the CLI and Phase 5 for when to actually run it.

---

## Repository changes at a glance

### New modules (`src/options_m/`)

| File | Content |
| --- | --- |
| `mcp_client.py` | `AlpacaMcp` facade. `fastmcp.Client` over `StdioTransport` running `alpaca-mcp-server` as a subprocess; long-lived session, reconnect, retry, timeout. **The only module that touches Alpaca.** Refuses write tools in `dry_run` |
| `earnings.py` | Hand-maintained earnings-date dict for the fixed universe + `is_earnings_blackout()`. **Done** — see Phase 2/3 |
| `llm.py` | Featherless client (`httpx` → `POST {base}/chat/completions`). **One tier now** (`model_deep` only — there is no separate fast-tier analyst crew to serve). Pydantic-validated structured output, one repair retry, then **fail-closed** (no trade) |
| `evidence.py` | Evidence-pack collector (technical only — no news), compact JSON serialisation, `NO_DATA` sentinels |
| `strategist.py` (LLM read) + `matrix.py` (deterministic gate) | Trend/regime/thesis LLM call; Strategy Matrix + earnings gate lookup. See Phase 3 |
| `strategy_builder.py` | `StrategyIntent` → contract selection from the live chain → `OrderPlan` (legs, qty, limit price, max loss) for all 9 supported structures |
| `risk.py` | Deterministic guardrails, zero LLM |
| `store.py` | Postgres repository layer on top of `db.py`; in-memory fallback when the DB is disabled so local dev works |
| `schema.sql` + `migrate.py` | Table schema and an idempotent migration runner |
| `trading/*.py` | One module per Layer-1 agent (`market_pulse.py`, `strategist.py`, `execution.py`, `position_manager.py`, `reflection.py`) |
| `cli.py` | `options-m status \| propose --dry-run \| trade --once \| positions \| flatten` — cheap, since the logic lives in the modules, and a second piece of evidence for rule #2 |

### Existing files to change

- **`config.py`** — Alpaca (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER_TRADE=true`,
  `ALPACA_TOOLSETS` **without `news`**), Featherless (`FEATHERLESS_API_KEY`,
  `FEATHERLESS_BASE_URL=https://api.featherless.ai/v1`, `FEATHERLESS_MODEL_DEEP`),
  per-agent intervals, every risk limit, and the wind-down cutoff. Keep the existing
  `Field(...)` validation style. **Note:** the Phase-1 code already shipped with `news` in
  the default `ALPACA_TOOLSETS` — that one-line default needs to be corrected as part of
  Phase 2 (it is a real code/design drift now that this doc has been updated; flagged again
  in Phase 1's addendum below).
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
- **Earnings blackout** — reject a new position on a symbol inside its `earnings.py` window
- Daily-loss halt and drawdown-from-high-water-mark halt
- Kill switch: DB flag + `POST /admin/kill` + env; every agent checks it each iteration
- Market-hours gate **via the local `market_calendar` cache** — no per-call `get_clock`
- Wind-down cutoff: no new positions inside the pre-deadline window (config, Phase 4)
- Idempotency: `client_order_id = f"om-{proposal_id}"`, so one proposal can never place
  two orders (`VibeHedge` re-buys a put every 60 s while a breach persists)
- **Never fake a fill**: a failed order is written as `orders.status='failed'` and never
  reported as success (`VibeHedge` returns `FILLED_SIMULATED` with a hardcoded $3.50 premium)
- Every rejection is written to `risk_events` — strong dashboard material

### Postgres schema

`agent_runs` (per-iteration telemetry), `market_snapshots`, `candidates`, `market_calendar`
(local calendar cache, see above), `account` (local account/buying-power/options-level
cache), `positions` (local open-positions cache), `proposals` (evidence + LLM regime read +
matrix verdict as JSONB), `orders` (unique `client_order_id`), `fills`, `equity_curve`,
`risk_events`, `iv_history`, `lessons`, `kill_switch`.

### Dashboard (`api.py` + `static/`)

A single page with no build step (server-rendered HTML + `fetch` polling on three tiers:
2 s / 10 s / 60 s):

- Equity curve + daily P/L, open option positions (greeks, DTE, P/L)
- Live agent status (last iteration, duration, error counter) from `agent_runs`
- **Decision timeline**: expanding a proposal reveals the evidence pack, the LLM's trend +
  volatility-regime read with thesis and invalidation, the Strategy Matrix + earnings-gate
  verdict, the selected real contracts, and the risk-gate result
- Risk-event feed and a kill-switch button
- `/api/*` JSON endpoints; `/health` and `/ready` stay exactly as they are

---

## Phase index

Work through them in order. Each doc carries enough context to be executed in a fresh session.

| Phase | Doc | Goal | Status |
| --- | --- | --- | --- |
| 1 | `phase-1-foundation.md` | Dependencies, config, `mcp_client.py`, `store.py` + schema, `MarketPulseAgent` running end to end. **Deploy to Render early** + UptimeRobot pinger | ✅ **Code complete**, with one known drift to fix in Phase 2 (see that doc's addendum) — 86 tests, ruff + strict mypy green. Deployment steps pending (need Neon/Render/Alpaca accounts) |
| 2 | `phase-2-evidence-risk-execution.md` | `evidence.py`, `earnings.py` (done), `strategy_builder.py` for all 9 structures, `risk.py`, `ExecutionAgent`, the local-cache tables and their write-owners | ⬜ Next — broken into 2.1–2.7, see the doc |
| 3 | `phase-3-strategist-agent.md` | `llm.py` + the single `StrategistAgent` (LLM regime read → deterministic Strategy Matrix + earnings gate). First real paper options order | ⬜ |
| 4 | `phase-4-position-reflection-dashboard.md` | `PositionManagerAgent`, `ReflectionAgent`, the dashboard, the `flatten` wind-down CLI | ⬜ |
| 5 | `phase-5-tests-live-run.md` | Tests, polish, **live paper run** so real trade history accumulates for the judges, then wind-down | ⬜ |
| 6 | `phase-6-submission.md` | Video, write-up, slides, metadata, deploy freeze | ⬜ |

(Phase 3's file was renamed from `phase-3-llm-crew.md` to `phase-3-strategist-agent.md` to
match the design: there is no crew anymore, one agent.)

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
2. Watch `MarketPulseAgent` populate `market_calendar` and `account` once, then
   `StrategistAgent` produce a proposal from a technical + IV/RV read.
3. Open the proposal: the regime read (trend, IV/RV, thesis, invalidation), the matrix
   verdict, and the selected real OCC contracts.
4. `ExecutionAgent` submits → the position appears in the Alpaca paper account and matches
   `orders` by `client_order_id`.
5. Hit the kill switch → agents stop producing new orders, and the event lands in `risk_events`.
6. `PositionManagerAgent` closes a position that hit its profit target →
   `ReflectionAgent` writes the lesson into `lessons`.
7. Near the deadline: `options-m flatten` closes every remaining open position one by one and
   exits non-zero if any close fails, so nothing is left unaccounted for at judging time.

---

## Accepted assumptions

- **Options level:** assume up to Level 3 on the paper account (options are enabled by
  default in Alpaca paper). If it turns out to be Level 2, multi-leg spreads are disabled by
  env flag and the matrix degrades to `long_call`/`long_put` (still directional, no vol-regime
  leg selection) plus `long_strangle` (both legs long, permitted at Level 2) — `risk.py`
  keeps this configurable.
- **Featherless model** is not hardcoded; chosen by env — a single deep-tier instruct model
  (there is no separate fast-tier analyst crew to serve now that the crew is gone).
- **Universe** is a fixed 10-symbol set: `SPY, QQQ, IWM` (ETFs, no earnings risk) plus
  `AAPL, MSFT, GOOGL, META, AMD, TSLA, NVDA` (mega-cap single names, tracked in
  `earnings.py`) — chosen for tight option-chain spreads and because it is exactly what
  `earnings.py` needs to stay small enough to hand-maintain.

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
  and `do_not_exercise_options_position` are permanently disabled. This is also why the
  wind-down policy uses a manual `options-m flatten` CLI rather than an automatic timer —
  even a scoped, allowed action (`close_position`) gets a human-triggered command instead of
  an unattended cron near the deadline.
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
- Hardcoded market-hours calendars instead of a properly refreshed local cache — the
  difference between this project's `market_calendar` table and a hardcoded holiday list is
  that ours is populated from `get_calendar` and refreshed daily, never typed in by hand.
- Constructing a new broker client on every call instead of caching it.
- Pure counter-based debate rounds with no early stop — cost scales with no benefit. (Moot
  now that there is no debate — one LLM call per `StrategistAgent` iteration — but keep this
  in mind if a future extension ever reintroduces multi-role reasoning.)
- Signal extraction by scanning the last 100 chars for BUY/SELL — "avoid a SELL here" parses as SELL.
