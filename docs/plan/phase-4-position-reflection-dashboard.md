# Phase 4 — Position management, reflection memory, judge-facing dashboard

**Master doc:** `hackathonda-in-a-etmen-istenen-shiny-clarke.md`
**Prerequisite:** Phase 3 complete — the crew produces proposals autonomously and
`ExecutionAgent` places real paper options orders.
**Goal:** close the loop. Positions get managed and exited, closed trades become lessons that
feed back into future prompts, and everything becomes visible in a dashboard a judge can
read in 60 seconds.

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

## Deliverables

### 1. `src/options_m/trading/position_manager.py` — `PositionManagerAgent`

Cadence 60 s. **Deterministic exit rules — no LLM.** Exits must keep working when the LLM
budget is exhausted, the model is down, or the kill switch is engaged. The kill switch stops
*opening*, never *closing*.

Each iteration:

1. `positions = await mcp.get_all_positions()`, filtered to option positions.
2. Match each to its originating proposal via `orders.client_order_id`.
3. Mark to market from `get_option_snapshot` / `get_option_latest_quote`; write
   `positions_history` and update the position's unrealised P/L.
4. Apply exit rules, first match wins, each configurable:

| Rule | Default |
| --- | --- |
| Profit target | close at +50% of debit paid |
| Stop loss | close at −50% of debit paid |
| DTE exit | close at ≤ 7 DTE regardless of P/L (gamma/theta risk) |
| Time stop | close after N days with no thesis progress |
| Thesis invalidation | close when the intent's `invalidation` condition is measurably true (only if it was expressed as a numeric level) |
| End-of-day blackout | never open, always allowed to close |

5. Closing uses `close_position` for the whole position, or a multi-leg closing order for
   verticals — one request, never leg out.

   **Closing is order entry, not deletion.** Alpaca's paper-trading skill (rule 10) is
   explicit: the close tools submit *market* orders, obey market hours, queue to the next open
   when the market is shut, and fill at an unknown price. So:
   - never report a position as flat because the close call returned — monitor the resulting
     order to a terminal state and only then write the `trades` row;
   - prefer an explicit multi-leg limit closing order via `place_option_order` with
     `position_intent="sell_to_close"` over `close_position`, so the exit is priced rather than
     thrown at the market. Fall back to `close_position` only when the priced exit will not
     fill and the DTE rule is forcing the issue;
   - `close_all_positions` is permanently disabled in `mcp_client.FORBIDDEN_TOOLS` and is not
     available as a panic button — closing is per position, deliberately.
6. Every close writes a `trades` row: entry, exit, realised P/L, holding period, exit reason,
   plus the originating `proposal_id`.

`VibeHedge` has no exit logic at all and re-buys a hedge every 60 s while a breach persists.
Position awareness and idempotency are what separate a demo from a toy.

### 2. `src/options_m/trading/reflection.py` — `ReflectionAgent`

Cadence hourly, plus one run after the close. This is the "it learns" story and it is cheap.

1. Find `trades` rows with `reflected = false`.
2. For each, build a compact record: the original evidence highlights, the PM thesis, what
   actually happened (realised P/L, holding period, exit reason), and the underlying's move
   over the holding period.
3. One fast-tier LLM call using `prompts/reflection.md`, capped at **2–4 sentences** — the
   `TradingAgents` reflection prompt caps length for exactly this reason
   (`graph/reflection.py:20-29`): a lesson is only useful if it is cheap to re-inject.
4. Write to `lessons(id, ts, symbol, trade_id, lesson, outcome_pct, tags)`.
5. `store.recent_lessons(symbol, n)` returns same-symbol lessons first, then a couple of
   cross-symbol ones — the `n_same=5, n_cross=3` shape from
   `TradingAgents/.../utils/memory.py:70-95`. Phase 3's `StrategistAgent` already calls this.

**No vector store.** Plain SQL over a `lessons` table is simpler, human-auditable, has no
embedding dependency or cost, and reads better on stage. `AlpacaTradingAgent` runs five
ChromaDB collections and it is the most fragile part of that repo (it even keeps file handles
open on Windows).

Failure-isolate the whole agent: a reflection failure must never affect trading.

### 3. Dashboard — `api.py` + `src/options_m/static/`

Single page, no build step. Server-rendered HTML shell plus vanilla JS `fetch` polling on
three tiers — 2 s for agent status, 10 s for proposals and positions, 60 s for account and
equity. (This is the tiered-interval shape `AlpacaTradingAgent` uses in Dash, and it is the
right trade-off for a demo: no websocket to drop mid-presentation.)

Sections, in the order a judge should read them:

1. **Header** — account equity, day P/L, total P/L vs the $100k start, open positions count,
   market open/closed with the next open/close, dry-run badge, kill-switch toggle.
2. **Equity curve** — SVG line chart drawn from `equity_curve`. No charting library; a
   polyline over a scaled viewBox is enough and cannot break at demo time.
3. **Open positions** — underlying, structure, legs, DTE, delta, entry vs mark, unrealised
   P/L, the exit rule that will fire first.
4. **Decision timeline** — the centrepiece. Each proposal is one row: timestamp, symbol,
   verdict, conviction, status. Expanding it reveals the bull argument, the bear argument,
   the volatility read, the PM's JSON verdict with its thesis and invalidation, the selected
   real OCC contracts, the risk verdict, and the resulting order. **This is the single most
   persuasive artefact in the submission** — it proves the agent reasons rather than
   pattern-matches, and no reference project surfaces it.
5. **Risk events** — every rejection with its rule and detail. "Declined 14 trades, here is
   why" is a stronger story than the trades taken.
6. **Agent health** — one row per agent from `agent_runs`: last run, duration, consecutive
   failures, cadence.
7. **Closed trades and lessons** — realised P/L per trade with the lesson learned beside it.

Endpoints: `/api/status`, `/api/equity`, `/api/positions`, `/api/proposals`,
`/api/proposals/{id}`, `/api/risk-events`, `/api/agent-runs`, `/api/trades`,
`POST /api/kill-switch`. `/health` and `/ready` stay untouched.

Guard the kill-switch endpoint with a shared-secret header from config
(`admin_token`) — the service is on a public URL.

Visual direction: dark, dense, monospace numerals, one accent colour; it should read as a
trading terminal, not a template. Load the `frontend-design` skill before writing the CSS.
Everything must stay legible in a screen recording at 1080p — that is the actual delivery
medium.

### 4. Schema additions

`trades(id, proposal_id, symbol, strategy, opened_at, closed_at, entry_price, exit_price,
qty, realized_pnl numeric, exit_reason, reflected bool default false)`,
`lessons(id, ts, symbol, trade_id, lesson, outcome_pct numeric, tags text[])`,
`llm_calls(id, ts, role, tier, model, prompt_tokens, completion_tokens, latency_ms, ok, error)`.

---

## Tests

- `test_position_manager.py` — each exit rule fires at its threshold and not before; rule
  precedence is deterministic; the kill switch does **not** block closes; a vertical closes
  as one multi-leg order; a broker read failure raises rather than reading as flat.
- `test_reflection.py` — a closed trade produces exactly one lesson; a failing LLM call
  leaves `reflected = false` and does not raise out of `step()`; `recent_lessons` ordering is
  same-symbol first.
- `test_api.py` (extend the existing file) — every `/api/*` endpoint returns valid JSON with
  an empty database; `/api/proposals/{id}` returns the full argument chain;
  `POST /api/kill-switch` requires the admin token; `/health` and `/ready` are unchanged.

---

## Acceptance criteria

- [ ] A position that hits +50% is closed automatically and a `trades` row is written.
- [ ] A closed trade produces a lesson, and the next proposal for that symbol shows the
      lesson inside its PM prompt (assert via the persisted prompt or a debug field).
- [ ] The dashboard renders the full decision chain for a real proposal, end to end.
- [ ] The kill switch blocks new orders while letting an exit through.
- [ ] The dashboard is readable in a 1080p screen recording.
- [ ] `ruff check . && mypy && pytest` green.

---

## Traps

- Never let the kill switch or an exhausted LLM budget block an exit.
- Never leg out of a spread with two separate orders.
- Do not add a charting library or a frontend build step this late.
- Do not expose the kill switch without an auth token on a public URL.
- Do not let a reflection failure propagate into the trading path.
