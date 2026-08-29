# options-m — Architecture & Data Flows

> Current as of commit `b1a7ccd` (2026-08-29).  
> This document describes what is built and running, not what is planned.

---

## Overview

options-m is a supervised, persistent, auditable autonomous options-trading service.
Every autonomous decision stores its evidence pack, the LLM's regime read, the
deterministic strategy-matrix verdict, the risk-gate verdict, and the order outcome
in Postgres so a judge can replay any decision after the fact.

Five agents run concurrently in a single asyncio process, each in its own supervised
loop with exponential backoff. No LangGraph, no orchestration framework — the
supervisor in `agents/__init__.py` is ~40 lines and owns error isolation.

```
┌─────────────────────────────────────────────────────┐
│                  asyncio event loop                 │
│                                                     │
│  MarketPulseAgent    60 s   no LLM                  │
│  PositionManagerAgent 60 s  no LLM                  │
│  ExecutionAgent       30 s  no LLM                  │
│  StrategistAgent       5 m  one LLM call / tick      │
│  ReflectionAgent      60 m  LLM per lesson           │
└─────────────────────────────────────────────────────┘
         │ read/write          │ read/write
         ▼                     ▼
   Local Postgres          Alpaca MCP
   (Neon serverless)       (stdio subprocess)
```

**Alpaca MCP** is the only broker interface.
Every agent that needs market data calls it through `AlpacaMcp`.
No agent holds a raw HTTP client; no agent bypasses the MCP layer.

**Postgres** is the shared state bus between agents.
Every inter-agent dependency is a cache table read, never a direct call.
Each cache table has exactly one writer.

---

## Agent Responsibilities

| Agent | Cadence | LLM | MCP calls | Writes to |
|---|---|---|---|---|
| `MarketPulseAgent` | 60 s | ✗ | account, calendar, evidence per symbol | `account`, `market_calendar`, `evidence`, `candidates`, `equity_curve` |
| `PositionManagerAgent` | 60 s | ✗ | `get_all_positions` | `positions` |
| `ExecutionAgent` | 30 s | ✗ | account, snapshot, chain, `place_option_order` | `orders`, `proposals`, `risk_events` |
| `StrategistAgent` | 5 m | ✓ one call | **none** | `proposals`, `llm_calls` |
| `ReflectionAgent` | 60 m | ✓ per lesson | **none** | `lessons` |

---

## Local Cache Tables

Each table has exactly one writer. All other agents read from the cache instead of
hitting Alpaca directly.

| Table | Sole writer | Refresh | Staleness risk |
|---|---|---|---|
| `market_calendar` | `MarketPulseAgent` | Once at startup, refreshed when rolling window shrinks below margin | Won't catch an unscheduled circuit-breaker halt |
| `account` | `MarketPulseAgent` | Every 60 s tick | Up to ~60 s stale |
| `evidence` | `MarketPulseAgent` | Every 60 s, one row per universe symbol, overwritten in place | Up to ~60 s stale — StrategistAgent only runs every 5 m so this is never the limiting factor |
| `positions` | `PositionManagerAgent` | Every 60 s tick, including unrealized P&L | Up to ~60 s stale |
| `orders` | `ExecutionAgent` | Write-through on every state change | None |

---

## Data Flow: MarketPulseAgent (every 60 s)

```
MarketPulseAgent._run()
│
├── _ensure_calendar_fresh()
│     get_calendar (once, ~1yr forward window)
│     ──► market_calendar upsert
│
├── get_account_info + get_account_config
│     ──► account upsert (equity, buying_power, options_trading_level)
│     ──► equity_curve append
│
└── [market open?]
      │
      └── for symbol in universe (SPY, QQQ, IWM, AAPL, MSFT, …):
            EvidenceCollector.collect(symbol)
            │  get_stock_snapshot    → spot block
            │  get_stock_bars        → trend block  (SMA20/50, RSI14, ATR14, RV20d, 52w range)
            │  get_option_chain      → options block (IV ATM, IV rank/pctile, skew, spread)
            │  get_option_contracts  → open interest
            │  get_all_positions     → position block
            │  get_news              → untrusted_news block
            │
            pack["earnings_blackout"]     = is_earnings_blackout(symbol, today)
            pack["options_trading_level"] = account.options_trading_level
            │
            [trend block is dict?]  ──no──► skip (MISSING pack, nothing to reason from)
            │
            ──► evidence upsert
            ──► _score_from_evidence(pack)
                  RSI extremity:  |RSI14 - 50| / 10
                  Realised vol:   min(rv × 2, 1.5)
                  IV/RV edge:     min((iv/rv − 1) × 3, 3.0)  if iv/rv > 1.05
                  Earnings blackout → 0.0
            ──► candidates batch upsert (sorted by score desc)
```

---

## Data Flow: PositionManagerAgent (every 60 s)

```
PositionManagerAgent._run()
│
└── get_all_positions
      group legs by underlying OCC symbol
      for each underlying:
          legs[*].unrealized_pl  → summed
          legs[*].market_value   → summed (abs)
      ──► positions replace_positions (upsert open, delete closed)
          payload per symbol: {legs, unrealized_pl, market_value}
```

Mark-to-market P&L is written to the cache on every tick, not only at exit-check
time. The dashboard and StrategistAgent's pre-filter always read a fresh local value.

---

## Decision Flow: StrategistAgent (every 5 m)

**Zero MCP calls.** Every input is a local Postgres read; the only outbound I/O is
the single LLM request.

```
StrategistAgent._run()
│
├── market_is_open(now)                 ← market_calendar cache
├── kill_switch / llm_budget_exhausted?
│
├── top_candidates()                    ← candidates cache
│   filtered by:
│     is_earnings_blackout(symbol)      ← in-process, before reading evidence
│     symbol in open positions          ← positions cache
│     symbol in pending proposals       ← proposals table
│
├── get_cached_evidence(symbol)         ← evidence cache
│   stale? (age > 2 × market_pulse_interval) → skip
│
│                    ┌──────────────────────────────────────┐
│                    │  ONE LLM call (Featherless)          │
├── llm.complete_json(schema=RegimeRead)│                   │
│   system: strategist.md template      │  RegimeRead:      │
│   user:   evidence pack as JSON       │    thesis: str    │
│                                       │    invalidation: str
│   JSON extraction + 1 repair retry    │    conviction: float
│   fail → LlmContractError             │                   │
│        → proposals.status='llm_failed'└──────────────────-┘
│
└── matrix.decide(pack, regime)         ← pure code, no LLM
      │
      ├── earnings_blackout? → "hold"
      ├── classify trend:
      │     SMA20 > SMA50 and RSI > 55  → "up"
      │     SMA20 < SMA50 and RSI < 45  → "down"
      │     else                         → "flat"
      ├── classify IV regime:
      │     IV/RV ≥ 1.40  → "very_expensive"
      │     IV/RV ≥ 1.10  → "expensive"
      │     else           → "cheap"
      │
      ├── matrix lookup:
      │     (up,   expensive)      → put_credit_spread
      │     (up,   very_expensive) → put_credit_spread
      │     (up,   cheap)          → call_debit_spread
      │     (flat, expensive)      → iron_condor
      │     (flat, very_expensive) → iron_butterfly
      │     (flat, cheap)          → long_strangle
      │     (down, expensive)      → call_credit_spread
      │     (down, very_expensive) → call_credit_spread
      │     (down, cheap)          → put_debit_spread
      │
      ├── level degradation:
      │     effective_level = min(account.options_trading_level, settings.options_level)
      │     Level < 3, debit spread  → long_call / long_put
      │     Level < 3, credit/condor → "hold"
      │
      └── conviction < floor (0.55)  → "hold"

      "hold"        → proposals.status = 'no_action'
      StrategyIntent → proposals.status = 'pending'
                       (llm_read + matrix_verdict stored as JSONB)
```

---

## Execution Flow: ExecutionAgent (every 30 s)

```
ExecutionAgent._run()
│
├── kill_switch?
│
└── pending_proposals(limit=5)
      for each proposal:
        │
        ├── StrategyIntent.model_validate(intent)     ← typed parse
        ├── intent.action == "hold" → status='held', skip
        │
        ├── get_account_info                           ← live MCP
        ├── get_stock_snapshot(underlying)             ← live MCP (spot price)
        ├── get_option_contracts(underlying, dte range)← live MCP
        ├── get_option_chain(underlying, dte range)    ← live MCP
        ├── get_open_position(underlying)              ← live MCP
        │
        ├── strategy_builder.build(intent, contracts, snapshots, …)
        │     selects real OCC contracts closest to target delta + DTE
        │     computes limit_price, max_loss, breakeven
        │     → OrderPlan  or  Rejection
        │
        ├── RiskEngine.evaluate(plan, portfolio)       ← pure code
        │     max_premium_pct_per_trade
        │     max_total_premium_pct
        │     max_concurrent_positions
        │     max_positions_per_underlying
        │     DTE window (7–45)
        │     min_open_interest, max_spread_pct
        │     earnings_blackout
        │     daily_loss_halt (3% of equity)
        │     drawdown_halt (8% from high-water mark)
        │     kill_switch
        │     already_submitted (idempotency)
        │     → RiskVerdict {approved, reasons}
        │
        ├── [dry_run] → status='dry_run_approved', no order
        │
        └── place_option_order(**request)              ← live MCP
              duplicate client_order_id → reconcile, not retry
              → orders.status='submitted'
              → proposals.status='submitted'

      _reconcile():
        orders_in_flight() → get_order_by_client_id() → update status
```

---

## Reflection Flow: ReflectionAgent (every 60 m)

```
ReflectionAgent._run()
│
├── Pass A — Closed trades
│     recent_orders(status='filled')
│     for each order not yet in lessons (reflected_on='order:{id}'):
│       llm.chat_completion(filled qty, avg price, legs)
│       → lesson text (1-2 sentences)
│       → save_lesson(source='closed_trade', reflected_on='order:{id}')
│
└── Pass B — Held / rejected proposals
      recent_proposals(status='no_action' | 'rejected')
      for each proposal not yet reflected on:
        llm.chat_completion(underlying, thesis, conviction, rejection reason)
        → lesson: was this hold a miss or a save?
        → save_lesson(source='held_proposal' | 'rejected_proposal',
                      reflected_on='proposal:{id}')

Lessons feed back into EvidenceCollector._lessons()
→ injected into the next StrategistAgent evidence pack for that symbol
```

---

## LLM Client

`FeatherlessLlm` (`llm.py`) — OpenAI-compatible, plain `httpx`, no SDK.

```
FeatherlessLlm.complete_json(schema=T, system, user, max_tokens, temperature)
  │
  ├── attempt 1: POST /chat/completions
  │     _extract_json(response)   ← code-fence and brace-depth extraction
  │     T.model_validate(data)
  │
  ├── [validation error] attempt 2 (repair):
  │     append original output + error to messages
  │     re-prompt
  │     T.model_validate(data)
  │
  └── [still failing] → raise LlmContractError
        StrategistAgent catches → proposals.status='llm_failed'
        Never falls back to free text for a trade decision.

Daily token budget: tracked in-memory, resets at UTC midnight.
Exhausted → StrategistAgent skips (PositionManagerAgent is never halted).
```

---

## Safety Layers

| Layer | Where | What it stops |
|---|---|---|
| `dry_run=True` | MCP transport | All write tools blocked at the subprocess boundary, not at call sites |
| `kill_switch` | Every agent, every tick | DB flag + env var + `POST /admin/kill`; immediately halts new orders |
| `RiskEngine` | `ExecutionAgent` | Premium cap, position cap, DTE, spread width, earnings, daily loss, drawdown |
| Earnings gate | `matrix.decide()` | Fires before matrix lookup; a blacked-out symbol never reaches the LLM |
| `LlmContractError` | `StrategistAgent` | Two failures → `llm_failed`, no trade, no exception to supervisor |
| `client_order_id = "om-{proposal_id}"` | `ExecutionAgent` | One proposal can never place two orders (broker-level idempotency) |
| Defined-risk only | `strategy_builder` + `RiskEngine` | Naked short legs rejected at two independent layers |
| FORBIDDEN_TOOLS | `AlpacaMcp` | `cancel_all_orders`, `close_all_positions`, `exercise_options_position` permanently disabled |

---

## Postgres Schema (key tables)

```sql
agent_runs        -- per-iteration telemetry (agent, ok, duration_ms, error, detail)
equity_curve      -- one point per MarketPulse tick (equity, cash, buying_power)
candidates        -- universe symbols ranked by evidence score per tick
evidence          -- current-state evidence pack per symbol (sole writer: MarketPulseAgent)
positions         -- current-state open positions with P&L (sole writer: PositionManagerAgent)
account           -- current-state account snapshot (sole writer: MarketPulseAgent)
market_calendar   -- ~1yr forward trading calendar (sole writer: MarketPulseAgent)
proposals         -- every StrategyIntent considered + llm_read + matrix_verdict JSONB
orders            -- every order attempt, client_order_id unique
risk_events       -- every risk-gate rejection with rule + detail
lessons           -- post-trade lessons from ReflectionAgent, keyed by reflected_on
llm_calls         -- LLM call log for token budget + dashboard
iv_history        -- per-symbol IV snapshots from EvidenceCollector
kill_switch       -- singleton row, toggled by dashboard or env
```

---

## Module Map

```
src/options_m/
│
├── agents/
│   ├── __init__.py          supervisor: Agent protocol, run_agent, build_agents
│   ├── market_pulse.py      MarketPulseAgent
│   ├── position_manager.py  PositionManagerAgent
│   ├── execution.py         ExecutionAgent
│   ├── strategist.py        StrategistAgent
│   └── reflection.py        ReflectionAgent
│
├── evidence/
│   ├── evidence.py          EvidenceCollector (assembles the per-symbol pack)
│   └── occ.py               OCC option-symbol parser
│
├── prompts/
│   ├── loader.py            path-escape-guarded template loader
│   └── strategist.md        StrategistAgent LLM prompt template
│
├── matrix.py                deterministic Strategy Matrix + earnings gate
├── llm.py                   FeatherlessLlm (chat_completion + complete_json)
├── models.py                StrategyIntent, RegimeRead, OrderPlan, Leg, Rejection
├── strategy_builder.py      StrategyIntent → OrderPlan (real contract selection)
├── risk.py                  RiskEngine (pure code, zero LLM)
├── store.py                 Postgres repository + in-memory fallback
├── mcp_client.py            AlpacaMcp (sole broker interface)
├── earnings.py              hand-maintained earnings-date dict + is_earnings_blackout()
├── config.py                env-driven Settings (pydantic-settings)
├── schema.sql               idempotent DDL applied at startup
├── migrate.py               schema runner
├── api.py                   FastAPI dashboard + /api/* endpoints
├── cli.py                   options-m status | propose | trade | flatten
├── chat.py                  read-only LLM Q&A for the dashboard
└── __main__.py              process entry point
```

---

## Configuration Reference (key knobs)

```
# Universe
UNIVERSE=SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMD,TSLA,META,GOOGL

# Safety
DRY_RUN=true                        # set false for live paper trading
KILL_SWITCH=false

# Agent cadences
MARKET_PULSE_INTERVAL_SECONDS=60
POSITION_MANAGER_INTERVAL_SECONDS=60
EXECUTION_AGENT_INTERVAL_SECONDS=30
STRATEGIST_INTERVAL_SECONDS=300
REFLECTION_INTERVAL_SECONDS=3600

# LLM
FEATHERLESS_API_KEY=…
FEATHERLESS_MODEL_DEEP=…            # never hardcoded; set by env
LLM_TIMEOUT_SECONDS=30
LLM_MAX_TOKENS=1024
LLM_DAILY_TOKEN_BUDGET=100000

# Strategy matrix
CONVICTION_FLOOR=0.55               # below this → hold regardless of matrix
OPTIONS_LEVEL=3                     # cap; effective = min(account_level, this)
SHORT_DELTA_DEFAULT=0.25
SPREAD_WIDTH_DEFAULT=5.0
DTE_TARGET_MIN=21
DTE_TARGET_MAX=38

# Risk limits
MAX_PREMIUM_PCT_PER_TRADE=0.02
MAX_TOTAL_PREMIUM_PCT=0.15
MAX_CONCURRENT_POSITIONS=5
MAX_POSITIONS_PER_UNDERLYING=1
RISK_DTE_MIN=7
RISK_DTE_MAX=45
DAILY_LOSS_HALT_PCT=0.03
DRAWDOWN_HALT_PCT=0.08
```
