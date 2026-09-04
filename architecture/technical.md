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
| `MarketPulseAgent` | 60 s | ✗ | account, calendar, snapshot, bars, chain, contracts, option bars, positions, news | `account`, `market_calendar`, `evidence`, `candidates`, `equity_curve`, `iv_history` |
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
            │                           iv_percentile, iv_history_sessions,
            │                           put_call_skew, atm_call/put greeks)
            │                           iv_rank/iv_percentile are daily statistics over
            │                           252 sessions of iv_history, MISSING below 126;
            │                           iv_backfill reconstructs the missing sessions
            │                           from historical option bars.
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
        ├── sizing.build_sizing_state(account)          ← pure code + equity_curve
        │     high-water mark, campaign window, options buying power
        │
        ├── strategy_builder.build(intent, contracts, snapshots, sizing_state, …)
        │     selects OCC contracts closest to target delta + DTE
        │     computes limit_price, max_loss, breakeven
        │     sizing.size_position() → qty        ← pure code
        │     → OrderPlan  or  Rejection
        │
        ├── build_portfolio_snapshot()
        │     get_clock() + get_all_positions()         ← live MCP
        │
        ├── RiskEngine.evaluate(plan, portfolio)        ← pure code
        │     premium cap per trade
        │     total premium cap
        │     options buying power (collateral the account can post)
        │     beta-weighted delta + net vega (whole book, plan included)
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

## sizing

All three construction shapes call one function — `sizing.size_position` — for
the contract count. It is pure, deterministic, and reads neither the clock nor
the broker: everything state-dependent arrives in a `SizingState`.

```
risk_fraction = base_risk_pct_per_trade      (0.015)
              × drawdown_scalar              [0.35 … 1.00]
              × gain_scalar                  [1.00 … 1.60]
              × conviction_scalar            [0.60 … 1.50], shrunk by reliability
              × horizon_scalar               {0, 1.00}   (front-load off by default)
              ↓ clamped to max_premium_pct_per_trade (0.20)

qty = min(
    risk_fraction × equity            ÷ max_loss_per_contract,
    options_buying_power × 0.50       ÷ collateral_per_contract,
    cash                              ÷ (strike × 100)   ← cash-secured put only
)
```

| Scalar | Source | Direction |
|---|---|---|
| `drawdown` | worse of equity-vs-high-water-mark and equity-vs-previous-close, each measured against its own halt threshold | Down as losses accumulate, floored at `drawdown_size_floor` so a recovery stays tradeable |
| `gain` | equity vs the campaign's opening equity | Up to `gain_size_cap`, one-directional |
| `conviction` | `StrategyIntent.conviction`, mapped across `[conviction_floor, 1.0]`, then shrunk toward 1.0 by the measured reliability | Bet size proportional to stated edge — but only as far as that edge has shown up in P&L |
| `horizon` | sessions left in the campaign, counted from `market_calendar` | `0` once too few remain → `campaign_horizon_closed` |

**This is not a martingale.** Every scalar moves size *down* after losses and
*up* after gains. Sizing up into a drawdown is how an account reaches its halt
threshold and stops being able to trade at all.

**Collateral, not premium.** Options are never marginable, so the account's
headline `buying_power` (2× equity on a margin account) is the wrong meter and is
deliberately absent from the resolution chain — `options_buying_power` →
`non_marginable_buying_power` → `cash`. Per contract, the requirement is
`max_loss` for every structure except a cash-secured put (`strike × 100`) and a
covered call (`0` — the shares are already paid for).

Buying power doubles as the aggregate-risk meter: every open defined-risk
structure already holds its max loss as collateral, so buying power falling *is*
open risk rising. That is why there is no separate portfolio-heat cap.

Unknown fields are handled two ways, on purpose. Unknown *capacity* (equity,
buying power) blocks the trade — approving because a broker field was unreadable
is the dangerous direction. Unknown *history* (no high-water mark, no campaign)
scales by 1.0 — a fresh database has genuinely observed no drawdown, and
refusing to trade until it has would mean the service can never place a first
order.

**Conviction is measured, not assumed.** Sizing by conviction is Kelly-flavoured
— bet in proportion to edge — but Kelly wants a *calibrated* probability, and
conviction is a language model's self-reported confidence with no prior claim to
predicting anything. `sizing.conviction_reliability` computes the Pearson
correlation between conviction and realised `pnl_pct` over closed trades, clamped
to `[0, 1]`, and shrinks the multiplier toward 1.0 by it. At zero reliability
every trade is sized the same, which is the right answer when the number carries
no signal.

The link needs no new writer: PositionManagerAgent already stamps the opening
`proposal_id` and a fresh `pnl_pct` onto each open position, and StrategistAgent
stores that payload as the close proposal's `evidence` — so a close proposal
carries both the P&L it was closed on and a pointer back to the conviction that
opened it. `store.conviction_outcomes()` joins the two.

Below `conviction_calibration_min_samples` (20) the answer is
`conviction_reliability_prior` (0.50), not 1.0: a short campaign closes something
like eight trades, a correlation over eight points is noise, and assuming full
predictive power on no evidence is the aggressive direction. Negative correlation
clamps to zero rather than inverting — "high conviction has lost money so far" is
a reason to stop leaning on the number, not to bet against the system's own
thesis on a handful of trades. ReflectionAgent's pass C reports the figure and
whether it is measured or still the prior, because a number that silently shrinks
every position is a number nobody will audit.

---

## exposure

Position counts and dollar-risk caps treat five positions as five independent
bets. In this universe they are not: SPY, QQQ, IWM and six large-cap tech names
correlate around 0.8-0.9, and the matrix reaches for the same structure family
across all of them whenever the IV regime is the same. A book of five
short-premium spreads is one short-vol, long-delta position wearing five hats.

Two aggregate measures, both checked in `RiskEngine` against the book *including*
the plan under evaluation — a cap that only measures what is already open would
approve the trade that breaches it:

| Measure | Unit | Cap | Rejection |
|---|---|---|---|
| Beta-weighted dollar delta | index-equivalent dollars | `max_beta_weighted_delta_pct` (1.00 × equity) | `beta_weighted_delta_exceeded` |
| Net vega | dollars per vol point | `max_net_vega_pct` (0.0075 × equity) | `net_vega_exceeded` |

Both compare on the absolute value: a book heavily short the index is as
directional as one heavily long, and the failure mode — every position losing
together on one move — does not care which way.

Both defaults are derived from `drawdown_halt_pct` (0.08) rather than picked, so
the caps and the breaker agree on the same worst case. At a delta cap of 1.00 a
maxed book loses ~1% of equity per 1% index move, so a 5% gap costs ~5% — inside
the halt. At a vega cap of 0.0075 a 10-point vol shock costs ~7.5% — the halt,
near enough.

Measured on the intended 5-position credit book at 10 DTE: 73% of equity in
beta-weighted delta, and 0.09% in vega. A credit structure's long wing offsets
most of its short's vega, so the vega cap never binds on a credit book; a
long-strangle book (both legs long vega, nothing offsetting) reaches 0.38-0.52%,
which is what that cap is actually for. Short DTE shrinks vega — it scales with
√T — which is why at a 7-14 DTE entry window **gamma, not vega, is the live
risk**, and why the delta cap is the one doing the work.

Broker positions carry no greeks at all, so each open leg's are recomputed from
its OCC symbol against the spot and ATM vol MarketPulseAgent cached for that
underlying — one local read per underlying, never a broker call. Betas are
hand-maintained in `exposure._BETA`, the same idiom as `earnings.py`; an unlisted
symbol is assumed *more* volatile than the index (1.50), never less, so an
unknown name overstates its own exposure and sizes the book down.

A leg whose greeks cannot be computed makes the whole aggregate unknown, never
zero — zero is indistinguishable from a perfectly hedged book and would read as
smaller. An unmeasurable book is refused (`unknown_portfolio_delta` /
`unknown_portfolio_vega`); an *empty* book is a measured zero and does not block
the first trade.

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
| `RiskEngine` | `ExecutionAgent` | Premium cap, buying power, position cap, DTE, spread width, earnings, daily loss, drawdown |
| Drawdown taper | `sizing.size_position` | Sizes every trade down as equity falls, so the halts are a backstop rather than the first line |
| Portfolio greeks | `RiskEngine` + `exposure` | Beta-weighted delta and net vega across the whole book: five correlated positions read as one bet, not five slots |
| Conviction shrinkage | `sizing.conviction_reliability` | Size leans on conviction only as far as conviction has measurably predicted P&L |
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
├── sizing.py                State-aware contract count — pure code, no LLM, no MCP
├── exposure.py              Beta-weighted delta + net vega — pure code, no LLM, no MCP
├── risk.py                  RiskEngine — pure code, no LLM, no MCP
├── store.py                 Postgres repository + in-memory fallback
├── mcp_client.py            AlpacaMcp — sole broker interface
├── earnings.py              Hand-maintained earnings calendar + is_earnings_blackout()
├── indicators.py            SMA · RSI · ATR · RV · 52-week range
├── volatility.py            IV rank · IV percentile · implied vol (Black-Scholes)
├── iv_backfill.py           Rebuilds a trading year of daily ATM IV from option bars
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

**Total premium cap mixes two units**
`RiskEngine._check_total_premium` compares the new plan's `max_loss` against the
sum of open positions' `|market_value|`. For a short credit spread those differ
substantially — market value is the premium still owed, max loss is the width
minus the credit — so the 15% aggregate cap is looser in practice than it reads.
Sizing does not depend on it (buying power is the aggregate meter there), but the
cap itself should be restated in risk terms.

**No end-of-campaign flatten**
`campaign_start_date` / `campaign_days` stop sizing from *opening* once too few
sessions remain, but nothing closes what is already open when the window ends.
`exit_time_stop_days` is cut to the campaign length (3) as an approximation, and
it is only that: it counts calendar days from the fill, so a position opened on
the first session exits a day *after* a three-session campaign ends. A campaign
that must end flat needs a PositionManagerAgent rule keyed on the campaign
window, not a duration backstop.

**Gamma is unmeasured**
`exposure` computes beta-weighted delta and net vega. At the configured 7-14 DTE
entry window gamma is the dominant risk — delta itself moves fast enough that a
cap on the *current* delta understates a gap through a short strike — and there
is no gamma budget. The delta cap plus the per-family stop loss stand in for one.

**Betas are hand-maintained and static**
`exposure._BETA` is a fixed table. Realised betas move, especially for the
high-beta single names, and a stale value understates exposure in exactly the
regime where correlation matters most. Deriving them from stock bars would mean a
returns regression per symbol; the table is right to about a tenth, which is
enough for a cap but not for hedging.

**Conviction calibration needs a season, not a campaign**
`conviction_reliability` needs 20 closed trades before it measures anything. A
three-session campaign produces roughly eight, so a short run will always size on
`conviction_reliability_prior` (0.50) rather than on measured data. The
measurement is there to compound across campaigns, not to help inside one.

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
DTE_TARGET_MIN=7                     # matched to the holding period; see config.py
DTE_TARGET_MAX=14
EXIT_DTE_SHORT_PREMIUM=3             # MUST stay below DTE_TARGET_MIN
EXIT_TIME_STOP_DAYS=3                # holding duration, cut to the campaign length

# Risk limits
MAX_PREMIUM_PCT_PER_TRADE=0.20
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

# Dynamic position sizing
BASE_RISK_PCT_PER_TRADE=0.015        # starting point; scaled, then clamped to the cap above
BUYING_POWER_UTILIZATION_CAP=0.50    # share of options buying power one trade may tie up
MAX_BETA_WEIGHTED_DELTA_PCT=1.00     # index-equivalent dollar delta, whole book
MAX_NET_VEGA_PCT=0.0075              # dollars per vol point, whole book
DRAWDOWN_SIZE_FLOOR=0.35             # smallest multiple a drawdown can taper to
GAIN_SIZE_CAP=1.60                   # largest multiple gains can fund
GAIN_SIZE_REFERENCE_PCT=0.04         # campaign gain at which the cap is reached
CONVICTION_SIZE_MIN_MULT=0.60
CONVICTION_SIZE_MAX_MULT=1.50
CONVICTION_RELIABILITY_PRIOR=0.50    # trust in conviction before it is measured
CONVICTION_CALIBRATION_MIN_SAMPLES=20

# Campaign horizon (unset start date = no horizon pacing)
CAMPAIGN_START_DATE=
CAMPAIGN_DAYS=3
CAMPAIGN_FRONT_LOAD_MULT=1.00        # neutral: 1.0 is off
CAMPAIGN_MIN_SESSIONS_TO_HOLD=1      # no new opens while this many sessions or fewer remain
```
