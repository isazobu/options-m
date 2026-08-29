# Phase 4 — Position management, reflection memory, judge-facing dashboard

**Master doc:** `00-MASTER.md`
**Prerequisite:** Phase 3 complete — `StrategistAgent` produces proposals autonomously
(one LLM call per iteration, gated by the deterministic Strategy Matrix) and `ExecutionAgent`
places real paper options orders.
**Goal:** close the loop. Positions get managed and exited, closed trades become lessons that
feed back into future `StrategistAgent` iterations, everything becomes visible in a dashboard
a judge can read in 60 seconds, and the service has a deliberate, manually-triggered way to
flatten every position before the deadline.

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

1. `positions = await mcp.get_all_positions()`, filtered to option positions. **This agent is
   the sole writer of the local `positions` cache** (`00-MASTER.md`'s local-cache table) —
   upsert every current position into it here, and remove rows for anything that dropped out
   of the response since the last tick. `StrategistAgent`'s "already positioned" pre-filter
   and `ExecutionAgent`'s pre-order position check both read this table instead of calling
   `get_all_positions`/`get_open_position` themselves.
2. Match each to its originating proposal via `orders.client_order_id` (read from the local
   `orders` cache — no live `get_orders` call needed here either).
3. Mark to market from `get_option_snapshot` / `get_option_latest_quote`; update the
   position's unrealised P/L (kept on the same `positions` cache row from step 1, not a
   separate table — the file previously specified a `positions_history` table; that is
   folded into `positions.payload` plus the `trades` row written on close, so no extra
   history table is needed).
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

**Scope (2026-08-29 design change): this agent evaluates decisions from *both*
`StrategistAgent` and `ExecutionAgent`, not only closed-trade outcomes.** A closed trade's
realised P/L is the clearest signal, but it is not the only decision worth a lesson — a
`hold` that would have worked, or a `risk.py` rejection that dodged a loser, are both
information `StrategistAgent` should eventually see again. Two passes per iteration:

**Pass A — closed trades (unchanged from the original design):**

1. Find `trades` rows with `reflected = false`.
2. For each, build a compact record: the original evidence highlights, the `StrategistAgent`
   thesis from `proposals.llm_read`, what actually happened (realised P/L, holding period,
   exit reason), and the underlying's move over the holding period.
3. One LLM call using `prompts/reflection.md`, on the same single `featherless_model_deep`
   tier Phase 3 uses (there is no separate fast tier to reach for here — the two-tier design
   was retired along with the analyst crew), capped at **2–4 sentences** — the
   `TradingAgents` reflection prompt caps length for exactly this reason
   (`graph/reflection.py:20-29`): a lesson is only useful if it is cheap to re-inject.
4. Write to `lessons(id, ts, symbol, trade_id, lesson, outcome_pct, tags)`.

**Pass B — held and rejected proposals (new):**

1. Find `proposals` rows with `status IN ('no_action', 'rejected')` older than a configurable
   look-back window (e.g. 1-2 trading days — long enough for the underlying to have actually
   moved) and not yet reflected on (`reflected` gains the same boolean on `proposals` as on
   `trades`).
2. For each, compare the thesis/regime read (or the risk rule that rejected it) against what
   the underlying actually did afterward: did a `hold` on a marginal-conviction setup miss a
   move that would have paid off, or correctly sidestep one that reversed? Did a `risk.py`
   rejection (say, a credit spread that failed the 12% credit/width floor) save the account
   from a structure that would have gone underwater, or was the rule needlessly conservative?
3. One LLM call, same model tier, same length cap, writing a lesson tagged distinctly from a
   closed-trade lesson (e.g. `tags: ["hold-review"]` or `["risk-reject-review"]`) so the
   dashboard and `recent_lessons` can tell the two kinds apart.
4. This pass is explicitly lower-priority than Pass A — if the LLM budget is tight, Pass A
   (real trade outcomes) always runs first; Pass B is a nice-to-have that deepens the "it
   learns" story but must never compete with reflecting on an actual closed trade.

**Shared:**

- `store.recent_lessons(symbol, n)` returns same-symbol lessons first, then a couple of
  cross-symbol ones — the `n_same=5, n_cross=3` shape from
  `TradingAgents/.../utils/memory.py:70-95`. Per Phase 2's design change, this is read by
  `MarketPulseAgent` inside `evidence.collect()`, not by `StrategistAgent` directly — the
  lesson ends up in the cached evidence pack `StrategistAgent` reads, feeding the *next*
  regime read for that symbol.
- Failure-isolate both passes independently: a Pass B failure must never block Pass A, and
  neither pass may ever affect trading — see the existing rule below.

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
   verdict, conviction, status. Expanding it reveals the evidence pack (trend classification,
   IV/RV regime, earnings-blackout flag), the `StrategistAgent`'s regime read (thesis,
   invalidation, conviction — `proposals.llm_read`), the Strategy Matrix's verdict
   (`proposals.matrix` — which cell fired and why, or the earnings gate short-circuit), the
   selected real OCC contracts, the risk verdict, and the resulting order. **This is the
   single most persuasive artefact in the submission** — it proves the agent reasons rather
   than pattern-matches, and no reference project surfaces it.
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
`llm_calls(id, ts, role, model, prompt_tokens, completion_tokens, latency_ms, ok, error)` —
`tier` is dropped from the original column list since there is only one model tier now.
Also add `ALTER TABLE proposals ADD COLUMN IF NOT EXISTS reflected bool NOT NULL DEFAULT
false` — Pass B (above) needs the same "have I looked at this yet" flag on `proposals` that
`trades` already has. `lessons` gains a nullable `proposal_id bigint REFERENCES
proposals(id)` alongside its existing (now also nullable) `trade_id`: a Pass A lesson sets
`trade_id`, a Pass B lesson sets `proposal_id`, and exactly one of the two is ever set on a
given row — never both, never neither.

### 5. `options-m flatten` — the wind-down CLI

Added to `src/options_m/cli.py` alongside the Phase 2 subcommands. This is the operational
answer to `00-MASTER.md`'s "Operational window & wind-down" section: the run only has until
4 Sep 2026 15:00 UTC, and nothing should be left open and unmonitored at that point.

- `options-m flatten` (optionally `--dry-run` to print what it *would* close, `--yes` to
  skip a confirmation prompt when run non-interactively): reads the local `positions` cache,
  and for each open position calls `close_position(symbol)` — **one call per symbol, in a
  loop, never `close_all_positions`** (permanently forbidden per Phase 1's
  `FORBIDDEN_TOOLS`). Waits for each close order to reach a terminal state before moving to
  the next symbol (per the "closing is order entry" rule above), logs the result, and writes
  the normal `trades` row for each one so `ReflectionAgent` still produces a lesson from it.
- Exit code is non-zero if any position failed to close, so it is safe to script a check
  around it (`options-m flatten && echo "clean"`).
- This is **manually triggered, not an automatic timer** — per Alpaca's own skill, an
  unattended service should not take irreversible-feeling actions with nobody watching, and a
  human running one command a couple of hours before the deadline is a better tradeoff than a
  cron job that fires when nobody is available to notice if it misbehaves.
- Separately, `risk.py`'s wind-down cutoff rule (Phase 2 §2.6) stops `ExecutionAgent` from
  *opening* new positions starting ~2–3 hours before the deadline — `flatten` only needs to
  run once, after that cutoff, and its job is closing what already exists, not preventing new
  entries (that is `risk.py`'s job).

---

## Tests

- `test_position_manager.py` — each exit rule fires at its threshold and not before; rule
  precedence is deterministic; the kill switch does **not** block closes; a vertical closes
  as one multi-leg order; a broker read failure raises rather than reading as flat.
- `test_reflection.py` — Pass A: a closed trade produces exactly one lesson with `trade_id`
  set; a failing LLM call leaves `trades.reflected = false` and does not raise out of
  `step()`. Pass B: a `no_action`/`rejected` proposal older than the look-back window
  produces a lesson with `proposal_id` set and a distinct tag; a proposal younger than the
  look-back window is left alone; Pass B never runs (or is skipped) when it would compete
  with a pending Pass A lesson for a tight LLM budget; a Pass B failure does not block Pass A
  in the same iteration. Shared: `recent_lessons` ordering is same-symbol first, and mixes
  both lesson kinds correctly.
- `test_api.py` (extend the existing file) — every `/api/*` endpoint returns valid JSON with
  an empty database; `/api/proposals/{id}` returns the full argument chain;
  `POST /api/kill-switch` requires the admin token; `/health` and `/ready` are unchanged.

---

## Acceptance criteria

- [ ] A position that hits +50% is closed automatically and a `trades` row is written.
- [ ] The local `positions` cache reflects reality within one `PositionManagerAgent` tick,
      and `StrategistAgent`/`ExecutionAgent` never call `get_all_positions`/`get_open_position`
      directly (grep confirms it).
- [ ] A closed trade produces a lesson, and the next proposal for that symbol shows the
      lesson inside `StrategistAgent`'s prompt (assert via the persisted prompt or a debug
      field) — reaching it through the cached evidence pack `MarketPulseAgent` builds, not a
      direct `recent_lessons` call from `StrategistAgent`.
- [ ] A `hold` or `risk.py`-rejected proposal older than the look-back window produces a
      Pass B lesson distinguishable from a Pass A one.
- [ ] The dashboard renders the full decision chain for a real proposal, end to end,
      including the regime read and the matrix verdict.
- [ ] The kill switch blocks new orders while letting an exit through.
- [ ] `options-m flatten --dry-run` lists every open position with no MCP write call;
      `options-m flatten` against a seeded set of fake open positions closes each one and
      exits 0.
- [ ] The dashboard is readable in a 1080p screen recording.
- [ ] `ruff check . && mypy && pytest` green.

---

## Traps

- Never let the kill switch or an exhausted LLM budget block an exit.
- Never leg out of a spread with two separate orders.
- Do not add a charting library or a frontend build step this late.
- Do not expose the kill switch without an auth token on a public URL.
- Do not let a reflection failure propagate into the trading path.
- Do not wire `flatten` to `close_all_positions` "for simplicity" — it is permanently
  forbidden; loop over `close_position` per symbol even though it is more code.
- Do not make `flatten` run automatically on a timer — it is a deliberate, human-triggered
  command by design.
