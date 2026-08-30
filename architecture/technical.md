# options-m — Technical Reference

## Overview

options-m is an autonomous options-trading service: five agents run concurrently
in a single `asyncio` process, sharing state through Postgres. No orchestration
framework — the supervisor in `agents/__init__.py` is ~40 lines.

```
┌──────────────────────────────────────────────────────────────────┐
│                        asyncio process                           │
│                                                                  │
│   MarketPulseAgent      60 s  ──┐                                │
│   PositionManagerAgent  60 s  ──┤──► Postgres (Neon serverless)  │
│   ExecutionAgent        30 s  ──┤       cache tables             │
│   StrategistAgent        5 m  ──┤       proposals / orders       │
│   ReflectionAgent       60 m  ──┘       lessons                  │
│                                                                  │
│   FastAPI  :8080  ───────────────────► dashboard / /api/*        │
└──────────────────────────────────────────────────────────────────┘
           │  stdio subprocess
           ▼
     Alpaca MCP Server
           │
           ▼
     Alpaca Paper Account
```

**Invariants:**
- `AlpacaMcp` is the only broker interface. No agent holds a raw HTTP client.
- Postgres is the shared state bus. Inter-agent dependencies are cache-table reads, never direct calls.
- Each cache table has exactly one writer.

---

## Agent Table

| Agent | Cadence | LLM | MCP calls | Writes to |
|---|---|---|---|---|
| `MarketPulseAgent` | 60 s | ✗ | account, calendar, snapshot, bars, chain, contracts, positions, news | `account`, `market_calendar`, `evidence`, `candidates`, `equity_curve`, `iv_history` |
| `PositionManagerAgent` | 60 s | ✗ | `get_all_positions` | `positions` |
| `ExecutionAgent` | 30 s | ✗ | account, snapshot, chain, contracts, position, place_order, get_order | `orders`, `proposals`, `risk_events` |
| `StrategistAgent` | 5 m | ✓ one call | **none** | `proposals`, `llm_calls` |
| `ReflectionAgent` | 60 m | ✓ per lesson | **none** | `lessons` |

---

## Cache Tables

| Table | Writer | Refresh | Staleness |
|---|---|---|---|
| `market_calendar` | `MarketPulseAgent` | Once at startup; refreshed when rolling window shrinks below margin | Won't catch an unscheduled circuit-breaker halt |
| `account` | `MarketPulseAgent` | Every tick | ~60 s |
| `evidence` | `MarketPulseAgent` | Every tick, per symbol, overwritten in place | ~60 s — StrategistAgent runs every 5 m, never the bottleneck |
| `positions` | `PositionManagerAgent` | Every tick, including unrealized P&L | ~60 s |
| `orders` | `ExecutionAgent` | Write-through on every state change | None |

---

## Data Flow: MarketPulseAgent (every 60 s)

```
MarketPulseAgent._run()
│
├── _ensure_calendar_fresh()
│     get_calendar(start, end)          ← ~1 year forward window
│     ──► market_calendar UPSERT  (no re-fetch until window margin is hit)
│
├── get_account_info() + get_account_config()
│     ──► account UPSERT  (equity, cash, buying_power, options_trading_level)
│     ──► equity_curve INSERT
│
└── [market open?]  ← market_calendar cache, no MCP
      NO ──► return early
      │
      YES
      └── for symbol in universe:
            EvidenceCollector.collect(symbol)
            ├── get_stock_snapshot    → spot block (bid/ask/last/spread/change_pct)
            ├── get_stock_bars        → trend block (SMA20/50, RSI14, ATR14, RV20d, 52w range)
            ├── get_option_chain      → options block (iv_atm at trading tenor, iv_rank,
            │                           iv_percentile, put_call_skew, atm_call/put greeks)
            ├── get_option_contracts  → open interest
            ├── get_all_positions     → position block
            └── get_news              → untrusted_news (truncated headlines)
            │
            pack["earnings_blackout"]     = is_earnings_blackout(symbol, today)
            pack["options_trading_level"] = account.options_trading_level
            │
            [no trend block] ──► skip (nothing to reason from)
            │
            ──► evidence UPSERT
            ──► _score_from_evidence(pack):
                  RSI extremity  = |RSI14 - 50| / 10
                  Realised vol   = min(rv × 2, 1.5)
                  IV/RV edge     = min((iv/rv − 1) × 3, 3.0)   (if iv/rv > 1.05)
                  Earnings blackout → 0.0
            ──► candidates batch INSERT (sorted score DESC)
```

---

## Data Flow: PositionManagerAgent (every 60 s)

```
PositionManagerAgent._run()
│
└── get_all_positions()
      group legs by underlying OCC symbol
      for each underlying:
          unrealized_pl  = Σ legs[*].unrealized_pl
          market_value   = Σ |legs[*].market_value|
      ──► positions REPLACE
            open underlyings  → UPSERT {legs, unrealized_pl, market_value}
            closed underlyings → DELETE
```

P&L is written every tick, not only at exit time. StrategistAgent's pre-filter
and the dashboard always read a fresh local value.

---

## Decision Flow: StrategistAgent (every 5 m)

Zero MCP calls. Every input is a local Postgres read. The only outbound I/O is
the single LLM request.

```
StrategistAgent._run()
│
├── market_is_open(now)                     ← market_calendar cache
├── kill_switch / llm_budget_exhausted?
│
├── top_candidates()                        ← candidates cache
│   filtered by:
│     is_earnings_blackout(symbol)          ← in-process (before reading evidence)
│     symbol in open positions              ← positions cache
│     symbol in pending proposals           ← proposals table
│
├── get_cached_evidence(symbol)             ← evidence cache
│   stale? (age > 2 × market_pulse_interval) → skip
│
│             ┌─────────────────────────────────────────┐
│             │  ONE LLM call (Featherless)             │
├── llm.complete_json(schema=RegimeRead)    │             │
│   system: "quantitative options           │ RegimeRead: │
│            strategist"                    │  thesis     │
│   user:   strategist.yaml + evidence JSON │  invalidation
│   JSON extraction + 1 repair retry        │  conviction │
│   fail → LlmContractError                 │  (0–1)      │
│        → proposals.status='llm_failed'    └─────────────┘
│
└── matrix.decide(pack, regime)             ← pure code, no LLM
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
      │     ┌────────────┬──────────────────┬────────────────┐
      │     │            │ IV expensive      │ IV cheap       │
      │     ├────────────┼──────────────────┼────────────────┤
      │     │ Trend up   │ put_credit_spread │ call_debit_sp  │
      │     │ Trend flat │ iron_condor *     │ long_strangle  │
      │     │ Trend down │ call_credit_sp    │ put_debit_sp   │
      │     └────────────┴──────────────────┴────────────────┘
      │     * IV/RV ≥ 1.40 → iron_butterfly
      │
      ├── options level degradation:
      │     effective = min(account_level, settings.options_level)
      │     level < 3, debit spread  → long_call / long_put
      │     level < 3, credit/condor → "hold"
      │
      └── conviction < 0.55 → "hold"

      "hold"         → proposals INSERT (status='no_action')
      StrategyIntent → proposals INSERT (status='pending', llm_read + matrix_verdict JSONB)
```

---

## Execution Flow: ExecutionAgent (every 30 s)

```
ExecutionAgent._run()
│
├── kill_switch? → return early
│
└── pending_proposals(limit=5)
      for each proposal:
        │
        ├── StrategyIntent.model_validate(intent)
        │   parse error → status='rejected', risk_events INSERT
        │
        ├── intent.action == "hold"  → status='held', skip
        ├── intent.action == "close" → _execute_close() (legs from positions cache)
        │
        ├── get_account_info()                          ← live MCP
        ├── get_stock_snapshot(underlying)              ← live MCP
        ├── get_option_contracts(underlying, dte_range) ← live MCP
        ├── get_option_chain(underlying, dte_range)     ← live MCP
        ├── get_open_position(underlying)               ← live MCP
        │
        ├── strategy_builder.build(intent, contracts, snapshots, …)
        │     selects OCC contracts closest to target delta + DTE
        │     computes limit_price, max_loss, breakeven
        │     → OrderPlan  or  Rejection
        │
        ├── build_portfolio_snapshot()
        │     get_clock() + get_all_positions()         ← live MCP
        │
        ├── RiskEngine.evaluate(plan, portfolio)        ← pure code
        │     premium cap per trade
        │     total premium cap
        │     max concurrent positions   (counts structures, not legs)
        │     max positions per underlying
        │     DTE window (7–45)
        │     min open interest
        │     max spread pct + max spread abs (both must be exceeded to reject)
        │     earnings blackout
        │     daily loss halt (3% of equity)
        │     drawdown halt (8% from high-water mark)
        │     kill switch
        │     already submitted (idempotency)
        │     → RiskVerdict {approved, reasons}
        │
        ├── dry_run=true → status='dry_run_approved'
        │
        └── place_option_order(**request)              ← live MCP
              duplicate client_order_id → reconcile
              success → orders INSERT (status='submitted')
                         proposals UPDATE (status='submitted')
              error   → orders INSERT (status='failed')
                         proposals UPDATE (status='failed')

      _reconcile():
        orders WHERE status='submitted'
        → get_order_by_client_id()   ← live MCP
        → orders UPDATE (status, filled_qty, filled_avg_price)
        broker-rejected/canceled/expired:
          → proposals UPDATE (status='broker_rejected', error=reason)
          → risk_events INSERT (rule='broker_rejected')
```

---

## Reflection Flow: ReflectionAgent (every 60 m)

```
ReflectionAgent._run()
│
├── Pass A — Closed trades
│     recent_orders(status='filled')
│     for each unreflected order:
│       llm.chat_completion("post-mortem analyst", filled qty, avg price, legs)
│       → 1-2 sentence lesson
│       → save_lesson(source='closed_trade', reflected_on='order:{id}')
│
└── Pass B — Held / rejected proposals
      recent_proposals(status='no_action' | 'rejected')
      for each unreflected proposal:
        llm.chat_completion(underlying, thesis, conviction, rejection_reason)
        → was this hold a miss or a save?
        → save_lesson(source='held_proposal'|'rejected_proposal',
                      reflected_on='proposal:{id}')

Lessons feed back via EvidenceCollector._lessons()
→ injected into StrategistAgent's next evidence pack for that symbol
```

---

## strategy_builder

All nine matrix strategies have a builder. Two construction shapes:

- **Single/vertical funnel** — `long_call`, `long_put`, `call_debit_spread`,
  `put_debit_spread`, `covered_call`, `cash_secured_put`. Anchor selected by
  delta; optional second leg snapped to `anchor ± width`.
- **Dedicated builders** — `long_strangle`, `put_credit_spread`,
  `call_credit_spread`, `iron_condor`, `iron_butterfly`. Each computes its own
  pricing and risk profile.

**Sign convention:** Alpaca multi-leg `limit_price` is positive = debit,
negative = credit. The net is carried positive throughout the module and
negated once when constructing `OrderPlan`.

**Wing width** scales with the expected move: `spot × IV × √(DTE/365)` using
the IV of the selected expiry — not a flat dollar amount. Two multipliers exist
because at-the-money short structures (iron butterfly, iron condor) need a
wider wing than delta-selected ones to leave a meaningful profit zone:

| Multiplier | Applies to |
|---|---|
| `spread_width_expected_move_mult` (0.45) | Delta-selected credit verticals |
| `spread_width_expected_move_mult_atm` (1.25) | At-the-money shorts (iron condor, iron butterfly) |

**Credit band** — both edges enforced, both written to `risk_events` on rejection:

| Edge | Setting | Default | Rejection |
|---|---|---|---|
| Floor | `min_credit_width_pct` | 0.12 | `thin_credit` |
| Ceiling | `max_credit_width_pct` | 0.70 | `credit_too_rich` |

---

## LLM Client

`FeatherlessLlm` (`llm.py`) — OpenAI-compatible, plain `httpx`, no SDK.

```
complete_json(schema=T, system, user, max_tokens, temperature)
  │
  ├── attempt 1: POST /chat/completions
  │     _extract_json()   ← code-fence then brace-depth extraction
  │     T.model_validate(data)
  │
  ├── [ValidationError] attempt 2 (repair):
  │     append original output + error to messages
  │     re-prompt
  │     T.model_validate(data)
  │
  └── [still failing] → raise LlmContractError
        StrategistAgent catches → proposals.status='llm_failed'
        Never falls back to free text for a trade decision.

Daily token budget tracked in-memory, resets at UTC midnight.
Exhausted → StrategistAgent skips. PositionManagerAgent is never halted.
```

---

## Domain Models

```python
class StrategyIntent:       # what the matrix decided — no real contracts yet
    action: "open" | "hold" | "close"
    strategy: Literal["long_call", "long_put", "call_debit_spread",
              "put_debit_spread", "put_credit_spread", "call_credit_spread",
              "long_strangle", "iron_condor", "iron_butterfly",
              "covered_call", "cash_secured_put"]
    underlying: str          # bare ticker
    target_delta: float      # 0 < δ ≤ 1
    spread_width: float | None
    dte_min: int
    dte_max: int
    conviction: float        # 0.0 – 1.0
    thesis: str
    invalidation: str

class RegimeRead:            # sole LLM output (StrategistAgent)
    thesis: str
    invalidation: str
    conviction: float        # 0.0 – 1.0

class Leg:                   # strategy_builder selects, never the LLM
    symbol: str              # real OCC symbol
    side: "buy" | "sell"
    ratio: int               # 1–4
    strike: float
    expiry: date
    option_type: "call" | "put"
    delta: float | None
    delta_source: "chain" | "black_scholes" | None

class OrderPlan:             # fully priced, risk-profiled plan
    proposal_id: int
    legs: list[Leg]          # 1–4 legs
    qty: int
    limit_price: float       # positive = debit, negative = credit
    max_loss: float          # finite, positive required
    client_order_id: str     # "om-{proposal_id}"

class Rejection:
    proposal_id: int
    reason: str
    detail: dict
```

---

## Safety Layers

| Layer | Where | What it stops |
|---|---|---|
| `dry_run=True` | MCP transport | All write tools blocked at the subprocess boundary |
| `FORBIDDEN_TOOLS` | `AlpacaMcp.call()` | `cancel_all_orders`, `close_all_positions`, `exercise_options_position` — permanently disabled |
| Paper assertion | `AlpacaMcp.connect()` | Refuses to connect unless `ALPACA_PAPER_TRADE` is set to a paper value |
| `kill_switch` | Every agent, every tick | DB flag + env var + `POST /admin/kill`; halts new orders immediately |
| Earnings gate | `matrix.decide()` | Fires before the matrix lookup; a blacked-out symbol never reaches the LLM |
| `RiskEngine` | `ExecutionAgent` | Premium cap, position cap, DTE, spread width, earnings, daily loss, drawdown |
| `LlmContractError` | `StrategistAgent` | Two failures → `llm_failed`, no trade, does not propagate to supervisor |
| `client_order_id = "om-{id}"` | `ExecutionAgent` | One proposal can never place two orders (broker-level idempotency) |
| Defined-risk only | `strategy_builder` + `RiskEngine` | Naked short legs rejected at two independent layers |
| Credit band | `strategy_builder` | `thin_credit` / `credit_too_rich`: no trade if risk/reward is outside bounds |

---

## Postgres Schema

```sql
-- Append-only telemetry
agent_runs     (agent, started_at, duration_ms, ok, error, detail JSONB)
equity_curve   (ts, equity, cash, buying_power, positions_count)
candidates     (ts, symbol, reason, score, payload JSONB)
iv_history     (ts, symbol, iv_atm, dte, spot, put_call_skew, term_structure, …)
risk_events    (ts, proposal_id→, rule, detail JSONB)
llm_calls      (ts, agent, model, prompt_tokens, completion_tokens, latency_ms, ok, error)
lessons        (ts, symbol, lesson, source, reflected_on UNIQUE)

-- Decision trail
proposals      (ts, underlying, status, intent JSONB, evidence JSONB,
                llm_read JSONB, matrix_verdict JSONB, plan JSONB,
                verdict JSONB, arguments JSONB, error)
orders         (proposal_id→, client_order_id UNIQUE, submitted_at, status,
                request JSONB, response JSONB, filled_qty, filled_avg_price, error)

-- Current-state cache (one writer, overwritten in place)
evidence       (symbol PK, payload JSONB, updated_at)
positions      (symbol PK, payload JSONB, updated_at)
account        (id=1 singleton, equity, cash, buying_power, options_trading_level, updated_at)
market_calendar(date PK, open TIMESTAMPTZ, close TIMESTAMPTZ, session_type)
kill_switch    (id=1 singleton, engaged, reason, updated_at)
```

---

## Module Map

```
src/options_m/
│
├── agents/
│   ├── __init__.py          Agent protocol · run_agent · build_agents (supervisor)
│   ├── market_pulse.py      MarketPulseAgent
│   ├── position_manager.py  PositionManagerAgent
│   ├── execution.py         ExecutionAgent
│   ├── strategist.py        StrategistAgent
│   └── reflection.py        ReflectionAgent
│
├── evidence/
│   ├── evidence.py          EvidenceCollector — assembles per-symbol pack
│   └── occ.py               OCC option-symbol parser
│
├── prompts/
│   ├── loader.py            Path-escape-guarded YAML prompt loader (Prompt.render)
│   ├── strategist.yaml      StrategistAgent regime-read (system + user) + trace
│   ├── chat.yaml            Read-only dashboard Q&A (system + untrusted-text warning)
│   ├── reflection.yaml      ReflectionAgent post-mortems (trade + proposal lessons)
│   └── llm_contract.yaml    complete_json schema suffix + repair-retry message
│
├── matrix.py                Deterministic Strategy Matrix + earnings gate
├── llm.py                   FeatherlessLlm — chat_completion + complete_json
├── models.py                StrategyIntent · RegimeRead · OrderPlan · Leg · Rejection
├── strategy_builder.py      StrategyIntent → real contract selection → OrderPlan
├── risk.py                  RiskEngine — pure code, no LLM, no MCP
├── store.py                 Postgres repository + in-memory fallback
├── mcp_client.py            AlpacaMcp — sole broker interface
├── earnings.py              Hand-maintained earnings calendar + is_earnings_blackout()
├── indicators.py            SMA · RSI · ATR · RV · 52-week range
├── volatility.py            IV rank · IV percentile · implied vol (Black-Scholes)
├── config.py                Env-driven Settings (pydantic-settings)
├── schema.sql               Idempotent DDL applied at startup
├── migrate.py               Schema runner
├── api.py                   FastAPI dashboard + /api/* endpoints
├── cli.py                   options-m status | propose | trace | plan
├── chat.py                  Read-only LLM Q&A for the dashboard
└── __main__.py              Process entry point
```

---

## Known Limitations

**IV neutral band (matrix gap)**
The IV regime has no "fairly priced, do nothing" band. Everything below IV/RV
1.10 is classified as "cheap" and triggers a long-premium structure. In a calm
tape this means the matrix selects `long_strangle` the majority of the time,
regardless of whether buying premium is justified.

**Wing snapping on non-uniform strike ladders**
`strategy_builder` computes snap tolerance from the *minimum* gap across the
whole expiry. Real ladders densify near the money and widen in the wings, so a
2.50 ATM tolerance rejects valid wing targets that are 5.00 apart. Fix: derive
tolerance from the local gap bracketing the target, not the global minimum.

**`buying_power` is not consulted**
The account cache stores `buying_power`, but sizing and risk decisions use only
`equity`. Nothing checks before submission that the account can carry the order.

**Clock reads in `strategy_builder` and `risk`**
`date.today()` / `datetime.now()` are called directly in several places.
`matrix.decide` is the exception (takes `as_of`). This is sound for a live
service but means the replay harness must swap module-level names to get
correct DTE calculations at past dates.

---

## Configuration Reference

```bash
# Universe
UNIVERSE=SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMD,TSLA,META,GOOGL

# Safety
DRY_RUN=true
KILL_SWITCH=false
ALPACA_PAPER_TRADE=true

# Agent cadences (seconds)
MARKET_PULSE_INTERVAL_SECONDS=60
POSITION_MANAGER_INTERVAL_SECONDS=60
EXECUTION_AGENT_INTERVAL_SECONDS=30
STRATEGIST_INTERVAL_SECONDS=300
REFLECTION_INTERVAL_SECONDS=3600

# LLM
FEATHERLESS_API_KEY=…
FEATHERLESS_MODEL_DEEP=…
FEATHERLESS_CHAT_MODEL=…
LLM_TIMEOUT_SECONDS=30
LLM_MAX_TOKENS=1024
LLM_DAILY_TOKEN_BUDGET=100000

# Strategy matrix
CONVICTION_FLOOR=0.55
OPTIONS_LEVEL=3
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
MIN_OPEN_INTEREST=100
MAX_SPREAD_PCT=0.10
DAILY_LOSS_HALT_PCT=0.03
DRAWDOWN_HALT_PCT=0.08
MINUTES_BEFORE_CLOSE_BLACKOUT=15
```
