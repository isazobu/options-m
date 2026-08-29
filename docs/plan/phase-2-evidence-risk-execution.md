# Phase 2 — Local caches, evidence, strategy construction, risk engine, execution

**Master doc:** `00-MASTER.md`
**Prerequisite:** Phase 1 complete — `AlpacaMcp` session live, `Store` writing to Postgres,
`MarketPulseAgent` running, service deployed with `DRY_RUN=true`.
**Goal:** produce a fully-formed, risk-approved **options order plan** from real chain data
and submit it — still no LLM. At the end of this phase a hand-written `StrategyIntent` goes
in and a real paper options order comes out, and the local-cache tables from `00-MASTER.md`
exist and are kept current.

This phase is where the hackathon's "options" requirement is actually satisfied, and where
all the safety credibility lives. Build it before the LLM (Phase 3), not after.

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

This phase is split into **seven numbered steps (2.1–2.7)**, each independently completable
and testable — given the short runway to the deadline, ship and test each one before moving
to the next rather than writing all seven files and testing at the end.

---

## 2.1 — Config fix + local-cache schema

Small, do this first; everything else in this phase reads from what it creates.

1. **Fix the `ALPACA_TOOLSETS` drift flagged in Phase 1's addendum.** In `config.py`, change
   ```python
   alpaca_toolsets: str = "account,trading,assets,options-data,stock-data,news"
   ```
   to
   ```python
   alpaca_toolsets: str = "account,trading,assets,options-data,stock-data"
   ```
   and remove `get_news` from `AlpacaMcp`'s typed convenience methods (Phase 1 added it;
   nothing will call it going forward).
2. **Add to `schema.sql`** (idempotent `CREATE TABLE IF NOT EXISTS`, as the file already
   does):
   ```sql
   CREATE TABLE IF NOT EXISTS market_calendar (
       date          date PRIMARY KEY,
       open          timestamptz NOT NULL,
       close         timestamptz NOT NULL,
       session_type  text NOT NULL DEFAULT 'full'
   );

   CREATE TABLE IF NOT EXISTS account (
       id                     smallint PRIMARY KEY DEFAULT 1,
       equity                 numeric,
       cash                   numeric,
       buying_power           numeric,
       options_trading_level  int,
       updated_at             timestamptz NOT NULL DEFAULT now(),
       CHECK (id = 1)
   );

   CREATE TABLE IF NOT EXISTS positions (
       symbol         text PRIMARY KEY,      -- underlying, not the OCC option symbol
       payload        jsonb NOT NULL,        -- full get_all_positions entry, verbatim
       updated_at     timestamptz NOT NULL DEFAULT now()
   );

   CREATE TABLE IF NOT EXISTS proposals (
       id          bigserial PRIMARY KEY,
       ts          timestamptz NOT NULL DEFAULT now(),
       underlying  text NOT NULL,
       status      text NOT NULL,            -- pending | dry_run_approved | rejected |
                                              -- submitted | llm_failed | no_action
       intent      jsonb,
       evidence    jsonb,
       llm_read    jsonb,                    -- StrategistAgent's trend/regime/thesis output
       matrix      jsonb,                    -- the deterministic matrix+gate verdict
       plan        jsonb,
       error       text
   );

   CREATE TABLE IF NOT EXISTS orders (
       id                 bigserial PRIMARY KEY,
       proposal_id        bigint REFERENCES proposals(id),
       client_order_id    text UNIQUE NOT NULL,
       submitted_at       timestamptz NOT NULL DEFAULT now(),
       status             text NOT NULL,
       request            jsonb,
       response           jsonb,
       filled_qty         numeric,
       filled_avg_price   numeric,
       error              text
   );

   CREATE TABLE IF NOT EXISTS risk_events (
       id           bigserial PRIMARY KEY,
       ts           timestamptz NOT NULL DEFAULT now(),
       proposal_id  bigint REFERENCES proposals(id),
       rule         text NOT NULL,
       detail       jsonb
   );

   CREATE TABLE IF NOT EXISTS iv_history (
       id          bigserial PRIMARY KEY,
       ts          timestamptz NOT NULL DEFAULT now(),
       symbol      text NOT NULL,
       iv_atm      numeric,
       rv_20d      numeric,
       iv_rv_ratio numeric
   );
   ```
   Money stays `numeric`. `positions` and `account` are **current-state caches** (upserted
   in place), everything else is append-only, matching the header comment already in
   `schema.sql` ("later phases add tables and columns; nothing here is ever rewritten" — the
   two cache tables are the deliberate exception, and are called out as such).
3. **`store.py` additions:** `upsert_market_calendar(rows)`, `market_is_open(at: datetime) ->
   bool` (pure local read — the one function every other agent's market-hours check now
   calls), `upsert_account(...)`, `get_cached_account()`, `upsert_position(symbol,
   payload)`, `remove_position(symbol)` (called when a position closes), `get_cached_positions()`,
   plus the Phase-1-planned `create_proposal`, `pending_proposals`, `record_order`,
   `recent_lessons` (stub returning `[]` until Phase 4), and `save_risk_event`.

**Test before moving on:** `test_store.py` — `market_is_open` returns the right answer for a
timestamp inside/outside a seeded calendar row and for a missing date (must be conservative:
missing row → treat as closed, never as open); `account`/`positions` upsert-then-read
round-trips; a second upsert overwrites rather than duplicating.

---

## 2.2 — `mcp_client.py`: calendar fetch + drop `get_clock` from the hot path

- Add `get_calendar(start: date, end: date) -> list[dict]` to `AlpacaMcp` (it is already in
  the `assets` toolset per Phase 1). `MarketPulseAgent` (Phase 1, revised — see its addendum)
  calls this once at startup for a ~1-year forward window and once daily thereafter, never
  per-iteration.
- `get_clock()` stays available on `AlpacaMcp` for the optional startup sanity-check
  mentioned in Phase 1's addendum, but audit every other call site in the codebase and
  replace it with `store.market_is_open(now())`.
- Remove `get_news` per 2.1.

**Test:** `test_mcp_client.py` gains a `get_calendar` fixture case; grep the codebase for
`get_clock(` outside `market_pulse.py` and the test file — there should be none.

---

## 2.3 — `src/options_m/earnings.py` — **done, no further work needed here**

Already implemented and pushed: a hand-maintained `EARNINGS: dict[str, EarningsDate]` for
the fixed universe's 7 non-ETF names (`AAPL, MSFT, GOOGL, META, AMD, TSLA, NVDA` — ETFs
carry no single-company earnings risk and are intentionally absent), each entry tagged
`confidence="confirmed"` or `"estimated"`, plus `next_earnings(symbol)` and
`is_earnings_blackout(symbol, as_of, days_before=3, days_after=1)`. Sourced 2026-08-29;
`LAST_REFRESHED` is a module constant — re-check every symbol before relying on this for a
run more than a few weeks old.

Alpaca exposes no earnings-calendar endpoint (`get_corporate_actions` /
`get_corporate_action_announcements` cover dividends, mergers, splits and spinoffs only —
confirmed against both OpenAPI specs bundled with `alpaca-mcp-server`), which is why this is
hand-maintained rather than fetched. See `00-MASTER.md`'s "Operational window & wind-down"
section for why this gate is coded but dormant for the current ~4.5-day run.

`risk.py` (2.5) and the Strategy Matrix (Phase 3) both call `is_earnings_blackout` — the
risk engine as a final backstop, the matrix as the primary filter before an LLM call is even
spent on a blacked-out symbol.

---

## 2.4 — Domain types (`src/options_m/models.py`) and `evidence.py`

### Domain types

```python
class StrategyIntent(BaseModel):
    """What the LLM's regime read plus the deterministic matrix together decide.
    The LLM never fills every field here itself — see Phase 3 for the split between
    the LLM's regime read and the matrix's structure choice."""
    action: Literal["open", "hold", "close"]
    strategy: Literal[
        "long_call", "long_put",                       # Level-2 fallback, single leg
        "call_debit_spread", "put_debit_spread",        # matrix: trend + cheap
        "put_credit_spread", "call_credit_spread",      # matrix: trend + expensive
        "iron_condor", "iron_butterfly",                # matrix: flat + expensive
        "long_strangle",                                # matrix: flat + cheap
    ]
    underlying: str
    target_short_delta: float | None   # credit structures: calibrates credit/width, see matrix
    target_delta: float | None         # single/debit structures: the long leg's target delta
    spread_width: float | None         # verticals/condor/butterfly, in strike points
    dte_min: int
    dte_max: int
    conviction: float                  # 0..1
    thesis: str
    invalidation: str                  # what would prove this wrong

class Leg(BaseModel):
    symbol: str                  # real OCC symbol taken from the chain, never constructed
    side: Literal["buy", "sell"]
    ratio: int = 1
    strike: float
    expiry: date
    option_type: Literal["call", "put"]
    delta: float | None
    bid: float | None
    ask: float | None
    open_interest: int | None

class OrderPlan(BaseModel):
    proposal_id: int
    underlying: str
    strategy: str
    legs: list[Leg]
    qty: int
    limit_price: float           # net debit (+) / credit (−) per contract, see sign convention below
    max_loss: float              # total dollars at risk, always finite
    max_profit: float | None
    breakeven: float | None
    client_order_id: str         # f"om-{proposal_id}" — idempotency key
```

`max_loss` being non-`None` and finite is the gate for "defined risk". If it cannot be
computed, the plan is rejected. Note `covered_call`/`cash_secured_put` from the original
design are **dropped** — this system never holds the underlying shares, every position is a
pure options structure, so there is no "covered" leg to build against.

### `evidence.py` — deterministic, technical only, no news

Collect via `AlpacaMcp` for one underlying, returning a compact dict well under the model's
context budget (target ≤ 4 KB of JSON):

- `get_stock_bars(symbol, timeframe="1Day", limit=60)` → compute **ourselves** (no
  `stockstats`/`pandas` dependency): SMA20, SMA50, ADX14, RSI14, ATR14, 20-day realized
  volatility (annualized stdev of daily log returns), distance from 52-week high/low.
- **Trend classification** from the above: `yukarı` when price/SMA20 are above SMA50 and
  ADX confirms trend strength; `aşağı` the mirror; `yatay` otherwise. This classification is
  itself pure code — it does not need the LLM, and Phase 3 explains exactly what is left for
  the model to do once trend and volatility regime are already computable deterministically.
- `get_option_chain(symbol, ...)` filtered to the DTE window → summarise into `iv_atm`,
  `put_call_skew`, `term_structure` (near vs far ATM IV), `median_spread_pct`,
  `total_open_interest`. Also compute `iv_rv_ratio = iv_atm / rv_20d` and classify: `pahalı`
  at ratio ≥ 1.10, `çok pahalı` at ratio ≥ 1.40, `ucuz` otherwise. Write a row to
  `iv_history` every time this runs (cheap, and it is what would eventually let `iv_rank`
  mature — see `00-MASTER.md` for why `iv_rank` itself is not trustworthy in this run's
  ~4.5-day window and must not gate a decision).
- **No `get_news` call, no `untrusted_news` field.** The evidence pack has no external-text
  field at all in this design.
- `is_earnings_blackout(symbol, today)` from `earnings.py` — included in the evidence pack as
  a plain boolean the matrix step reads directly; also checked by `risk.py` independently.
- `store.get_cached_positions()` — current position in this underlying, if any (local read,
  not `get_open_position`).
- `store.recent_lessons(symbol, n)` — Phase 4 fills this in; returns `[]` until then.

**Missing-data discipline unchanged:** any field we could not fetch becomes the literal
string `"NO_DATA_AVAILABLE"`, and the evidence pack carries a top-level note: `"Fields marked
NO_DATA_AVAILABLE are genuinely unavailable. Do not estimate or fabricate values."` Borrowed
from `TradingAgents/tradingagents/dataflows/interface.py:242`. This is what stops a model
from inventing an IV number.

Persist every pack into `proposals.evidence` (JSONB) so a judge can replay the decision.

#### Pack shape

`EvidenceCollector(settings, mcp, store).collect(symbol, dte_min=7, dte_max=45)` returns the
dict below. Every leaf is a number/int/str **or** the literal `"NO_DATA_AVAILABLE"`; a whole
section degrades to that string when its fetch fails, and the rest of the pack still returns
(the collector never raises for a single failed sub-fetch). Helpers live in
`src/options_m/occ.py` (`parse_occ_symbol` — read-only, no inverse) and
`src/options_m/indicators.py` (SMA / Wilder RSI / Wilder ATR / realised-vol / 52-week
distance, pure stdlib). IV-rank math stays in `options_m.volatility` via
`Store.iv_rank_for`; the collector adds no second implementation.

```
symbol            str          uppercased
as_of             str          UTC ISO-8601, "…Z"
note              str          the NO_DATA_AVAILABLE instruction (verbatim)
dte_window        [int, int]   [dte_min, dte_max]
spot              dict | "NO_DATA_AVAILABLE"
trend             dict | "NO_DATA_AVAILABLE"
options           dict | "NO_DATA_AVAILABLE"
position          list[dict] | null | "NO_DATA_AVAILABLE"   (null = confirmed flat,
                                                             the string = read failed)
lessons           list[str]                                 (always [] until Phase 4)
```

`spot` — from `get_stock_snapshot`:

```
bid / ask                     ← latestQuote.bp / .ap
bid_size / ask_size           ← latestQuote.bs / .as
mid  spread  spread_pct       derived
last                          ← latestTrade.p
day_open/high/low/close       ← dailyBar.o/h/l/c
day_volume  day_vwap          ← dailyBar.v / .vw
prev_close                    ← prevDailyBar.c
change_from_prev_close_pct    derived
quote_time                    ← latestQuote.t
```

`trend` — computed from ~252 daily bars (needed for a real 52-week range and a defined SMA50):

```
bars_used            int
sma_20  sma_50
rsi_14               Wilder
atr_14               Wilder, price units
atr_14_pct_of_spot
realised_vol_20d     annualised vol fraction (0.24 == 24%)
high_52w  low_52w
pct_from_52w_high    <= 0
pct_from_52w_low     >= 0
trend_label          "yukarı" | "aşağı" | "yatay"
```

`options` — from `get_option_chain` filtered to the DTE window and a ±15% strike band,
plus `get_option_contracts` joined by OCC symbol for open interest (the market-data chain
carries none):

```
dte_window           [int, int]
contracts_scanned    int
expiries_scanned     list[str]  ISO dates
near_expiry  near_dte
far_expiry   far_dte
iv_atm               mean of ATM call/put IV at the near expiry
iv_atm_near  iv_atm_far
iv_source            "chain" | "bsm_from_mid" | "NO_DATA_AVAILABLE"
iv_rank              volatility.iv_rank over iv_history; "NO_DATA_AVAILABLE" until 2 readings
iv_percentile        volatility.iv_percentile; same 2-reading guard
realised_vol_20d     the trend block's RV, carried here for side-by-side comparison
iv_rv_ratio          iv_atm / realised_vol_20d — vol regime: ≥1.40 çok pahalı, ≥1.10 pahalı, else ucuz
iv_minus_rv          iv_atm - realised_vol_20d — vol risk premium (>0 rich, <0 cheap)
put_call_skew        atm_put_iv - atm_call_iv
term_structure       iv_atm_far - iv_atm_near
median_spread_pct    across scanned contracts
total_open_interest  int
atm_call  atm_put    dict | "NO_DATA_AVAILABLE"
```

`atm_call` / `atm_put` (the contract nearest spot at the near expiry — a real OCC symbol
taken from the chain, never constructed):

```
symbol  strike  expiry  dte
bid  ask  mid  spread_pct
iv
delta  gamma  theta  vega
open_interest
```

`position` — from local `positions` cache, filtered to this underlying:

```
option leg:  kind="option", symbol, option_type, strike, expiry,
             side, qty, avg_entry_price, market_value, unrealized_pl, unrealized_plpc
equity:      kind="equity", symbol, side, qty, avg_entry_price,
             market_value, unrealized_pl, unrealized_plpc
```

`collect()` only reads and returns — it does not persist. Writing the pack into
`proposals.evidence` is done by whichever component owns the `proposals` row (the Phase 3
strategist, or `ExecutionAgent`). The one write `collect()` makes is the IV-history row
(`append_iv_snapshot`), once per pull, right before the rank is read.

**Test:** `test_evidence.py` — a failed sub-fetch yields `NO_DATA_AVAILABLE`, never a made-up
number; trend classification matches a hand-computed fixture; IV/RV classification hits all
three tiers (ucuz / pahalı / çok pahalı) at the right thresholds; `is_earnings_blackout`
correctly flows through from `earnings.py`.

---

## 2.5 — `src/options_m/strategy_builder.py` — all nine structures

`async def build(intent: StrategyIntent, chain, account) -> OrderPlan | Rejection`

The core anti-hallucination component: the intent names a strategy family and target
deltas/DTE window, never a contract. Steps, in order:

1. **Filter the live chain** to `intent.underlying`, the right option type(s), and expiries
   inside `[dte_min, dte_max]`. Prefer standard monthly expiries when several qualify.
2. **Select each anchor leg** by closest absolute delta to its target (`target_delta` for a
   long leg, `target_short_delta` for a short leg). If the chain snapshot carries no greeks,
   compute delta with Black-Scholes from mid price, spot, strike, DTE and implied vol — port
   `VibeHedge/src/options/options_lab.py:91-158`, which is clean and Alpaca-decoupled. Use
   the chain's greeks when present; BS is the fallback, and the plan records which was used.
3. **Per-structure leg construction:**
   - `long_call` / `long_put` — one leg, `buy_to_open`.
   - `call_debit_spread` / `put_debit_spread` — long leg at `target_delta`, short leg at
     `long_strike ± spread_width` (same expiry), snapped to the nearest listed strike. Reject
     if no listed strike is within one strike increment.
   - `put_credit_spread` / `call_credit_spread` — short leg at `target_short_delta`
     (calibrated per the table below), long protective leg at `short_strike ∓ spread_width`,
     same expiry. **The long leg is submitted in the same multi-leg order as the short leg —
     Alpaca's MLeg endpoint rejects a naked short in a multi-leg order, so this is enforced
     twice: here, and again in `risk.py`.**
   - `iron_condor` — put credit spread + call credit spread, same expiry, both short legs at
     `target_short_delta`, 4 legs total.
   - `iron_butterfly` — both short legs at the same at-the-money strike, long wings at
     `± spread_width`. Only ever constructed when the caller has already confirmed IV/RV ≥
     1.40 (Phase 3's matrix gate) — `strategy_builder` does not re-check the threshold, it
     trusts the intent, but `risk.py` still runs the credit/width check below as a backstop.
   - `long_strangle` — long call + long put, both at `target_delta` (or two independent
     deltas if the intent specifies both legs), same expiry. Both legs long — no naked-short
     concern, permitted at Level 2.
4. **Liquidity checks** (reject with a reason, do not "fix"): bid/ask present and > 0,
   `spread_pct = (ask-bid)/mid` under the configured cap, open interest above the minimum —
   for every leg.
5. **Pricing:** limit price = mid of the net structure, then nudged by a configurable
   fraction of the spread toward the aggressive side. **Never a market order** — options
   spreads are wide and `VibeHedge` sends market orders on every path.
6. **Structure-specific checks before accepting a plan** (see `00-MASTER.md`'s matrix section
   for the full rationale):
   - **Credit structures** (`put_credit_spread`, `call_credit_spread`, `iron_condor`,
     `iron_butterfly`): (a) the net is still a credit at the worst realistic fill; (b) credit
     ≥ **12%** of width (the calibrated floor — see the short-delta table below; 15% was
     tried and made the 0.20-delta setup unreachable); (c) chain IV > realized vol (the edge
     this structure is selling).
   - **Debit structures** (`call_debit_spread`, `put_debit_spread`): (a) the net is genuinely
     a debit; (b) paying ≤ **45%** of width; (c) reward/risk ≥ 1×.
   - **`long_strangle`**: no width concept — max loss is simply the premium paid; no
     credit/debit check applies.
7. **Compute `max_loss` analytically per structure.** Long single / long strangle: total
   debit paid. Debit vertical: net debit. Credit vertical / condor / butterfly:
   `spread_width × 100 × qty − credit_received`. If a structure would ever be undefined-risk,
   return a `Rejection` — this should be structurally impossible given step 3's leg
   construction, but the check stays as a backstop.
8. **Size:** `qty = floor(max_premium_budget / max_loss_per_contract)`, clamped by the
   position-count and per-underlying caps. `qty == 0` is a `Rejection`, not a silent no-op —
   `AlpacaTradingAgent`'s `int(amount/price)` silently placing nothing is exactly the bug we
   do not want.
9. `client_order_id = f"om-{proposal_id}"`.

### Short-delta → credit/width calibration (measured, on a 38-day chain)

| Short delta | Credit / width |
| --- | --- |
| 0.15 | ~10% |
| 0.20 | ~14% |
| 0.25 | ~18% |
| 0.30 | ~21% |
| 0.35 | ~27% |

Wing width barely moves this ratio; short delta is the lever. Keep this table as a code
comment next to the 12% threshold constant, so the next person tuning it does not have to
re-derive it. To get fatter credit, raise `SHORT_DELTA` in config, not the wing width.

### Sign convention (verified on real prices: iron condor → −1.51, debit spread → +1.84)

Alpaca's multi-leg `limit_price` is positive = debit, negative = credit. Our own
`net_worst` calculation is computed **positive when we are collecting a credit** (i.e. it
reads like a credit amount), so `limit_price = -net_worst` for credit structures and
`limit_price = +net_worst` (equivalently just `net_worst`) for debit structures — or more
simply: compute `net_worst` once with the "positive = credit received" convention
everywhere in the module, and negate it exactly once, at the point of building the
`place_option_order` payload, never earlier. Getting this backwards submits a credit spread
as if paying a debit for it (or the reverse) — write a unit test asserting the exact sign for
one fixture of each structure family, not just one example.

**Test:** `test_strategy_builder.py` — delta selection picks the right strike for every
structure; vertical/condor/butterfly width snapping; rejection when no strike is within one
increment; rejection on a wide spread / low open interest; `max_loss` correct for each of the
nine structures; BS fallback engages when greeks are absent; `qty == 0` returns a rejection;
the credit/width 12% floor and the debit 45%-of-width ceiling both reject the fixture chain
at the boundary; sign convention asserted for one credit and one debit structure against the
real numbers above (condor −1.51, debit spread +1.84).

---

## 2.6 — `src/options_m/risk.py` — the risk engine

Zero LLM, zero MCP writes, no imports from `strategist.py`/`llm.py`. Takes an `OrderPlan`
plus a locally-cached account/portfolio snapshot and returns `RiskVerdict(approved: bool,
reasons: list[str], adjusted_qty: int | None)`. Modelled on
`AlpacaTradingAgent/tradingagents/safety/guardrails.py`, which deliberately has no agent
imports so it can never be reasoned around.

Rules, each individually configurable and each individually unit-tested:

| Rule | Default | Reads from |
| --- | --- | --- |
| Max premium at risk per trade | 2% of equity | local `account` cache |
| Max total open option premium | 15% of equity | local `account` cache |
| Max concurrent option positions | 5 | local `positions` cache |
| Max positions per underlying | 1 | local `positions` cache |
| Defined risk only — reject any naked short leg | always on | the `OrderPlan` itself |
| DTE window | 7–45 | the `OrderPlan` |
| Min open interest per leg | 100 | the `OrderPlan` |
| Max bid-ask spread | 10% of mid | the `OrderPlan` |
| **Earnings blackout** | on, via `earnings.py` | `is_earnings_blackout(symbol, today)` |
| Daily loss halt | −3% of start-of-day equity | local `account` / `equity_curve` |
| Drawdown halt vs high-water mark | −8% | `equity_curve` |
| **Market must be open** | on | local `market_calendar` cache — **not** `get_clock` |
| Minutes-before-close blackout | 15 | local `market_calendar` |
| **Wind-down cutoff** — no new positions inside the pre-deadline window | on, ~2–3h before 4 Sep 15:00 UTC | config (see Phase 4) |
| Kill switch (env flag OR `kill_switch` table) | checked every call | `kill_switch` table |
| Idempotency: no existing order with this `client_order_id` | always on | local `orders` cache |

Note that **every "is X true right now" check reads a local table**, not a live Alpaca call
— this is the concrete payoff of the caching design in `00-MASTER.md`. The only Alpaca calls
left in the hot path by the end of this phase are `MarketPulseAgent`'s per-tick
account/equity call, `PositionManagerAgent`'s per-tick positions call, `StrategistAgent`'s
per-iteration chain/bars pull, and `ExecutionAgent`'s actual order submission — each owned by
exactly one agent.

Every rejection is written to `risk_events(ts, proposal_id, rule, detail jsonb)`. This table
is prime dashboard material — "the agent declined 14 trades and why" is a stronger judging
story than the trades it took.

Numeric hygiene: reuse the `_finite_float` semantics — a NaN high-water mark must not
permanently disable the drawdown breaker.

---

## 2.7 — `src/options_m/trading/execution.py` — `ExecutionAgent`

Cadence 30 s. No LLM.

```
step():
  1. if kill switch engaged: log, return
  2. proposals = store.pending_proposals(limit=5)
  3. for each:
       account, positions = store.get_cached_account(), store.get_cached_positions()  # local, no MCP call
       plan = strategy_builder.build(intent, chain, account)
       if Rejection: mark proposal 'rejected', write risk_event, continue
       verdict = risk.evaluate(plan, account, positions)
       if not approved: mark 'rejected', write risk_events, continue
       if settings.dry_run:
           mark 'dry_run_approved', persist the full plan, continue
       order = await mcp.place_option_order(...)   # legs, qty, limit, DAY, client_order_id
       store.record_order(plan, order, status='submitted')   # upsert into local `orders`
  4. reconcile: for orders in flight, get_order_by_client_id -> update fills/status in `orders`
```

**Order submission rules:**
- Always pass `client_order_id`. If Alpaca rejects it as a duplicate, that is a *success*
  path — the order already exists; reconcile via `get_order_by_client_id` instead of retrying.
- On any exception: `orders.status='failed'` with the error text. **Never** synthesise a
  fill. (`VibeHedge/src/execution/alpaca_trader.py:196-219` returns `FILLED_SIMULATED` with a
  hardcoded $3.50 premium — the single most dangerous pattern in the reference set.)
- Read the current position from the **local `positions` cache** before deciding — it is
  refreshed every 60 s by `PositionManagerAgent`, which is fresh enough for a per-30s
  execution loop. Fall back to a live `get_open_position` only if the cache has never been
  populated (e.g. immediately after a cold start).
- Multi-leg goes through `place_option_order` with all legs in one request, so the spread
  fills as one unit; never leg into a spread with separate orders — this is also a hard
  Alpaca API requirement for any structure with a short leg (see 2.5).

**`place_option_order` — exact contract** (read from
`../alpaca-mcp-server/src/alpaca_mcp_server/overrides.py:258-341`; this tool is a
hand-written override, so its schema is *not* in `tool_registry.py` and not in the OpenAPI
spec):

```
place_option_order(
    qty: str,                       # REQUIRED, string. Multi-leg: strategy multiplier —
                                    # each leg's ratio_qty is scaled by it
    type: str = "market",           # "market" | "limit"  → we always pass "limit"
    time_in_force: str = "day",     # "day" ONLY; options support nothing else
    symbol: str | None = None,      # OCC symbol, e.g. "AAPL250321C00150000" — single-leg only
    side: str | None = None,        # "buy" | "sell" — single-leg only
    position_intent: str | None = None,   # buy_to_open | buy_to_close | sell_to_open | sell_to_close
    limit_price: str | None = None, # string. Multi-leg: NET debit/credit —
                                    # positive = debit (cost), negative = credit
    client_order_id: str | None = None,
    order_class: str | None = None, # "mleg"; auto-inferred when legs is provided
    legs: list[dict] | None = None, # max 4 legs; each: {"symbol", "ratio_qty" (str),
                                    #                    optional "side", "position_intent"}
)
```

Consequences for `strategy_builder.py` and `execution.py`:

- **Everything numeric is a string.** `qty`, `ratio_qty` and `limit_price` are serialised as
  strings, not floats. Format them from `Decimal`, never from a Python `float` repr.
- **A spread is one order:** e.g. for `put_credit_spread`, `qty="1"`, `legs=[{"symbol":
  short_put, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
  {"symbol": long_put, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"}]`,
  `limit_price` = `-net_worst` (negative, a credit), and no parent `symbol`/`side`. An iron
  condor/butterfly is the same pattern with all 4 legs in one request. Do not set
  `order_class` — it is inferred from `legs`.
- Always pass `position_intent`. It is optional in the API but it is what makes a closing
  order unambiguous, and it is cheap insurance against accidentally opening a short leg.
- The override validates locally and returns an `{"error": ...}` dict (it does **not** raise)
  when a single-leg call is missing `symbol`/`side` or a multi-leg call is missing `legs`.
  So after every write call, check for an `error` key in the unwrapped payload and treat it
  as a failure — a returned dict is not proof of a submitted order.
- `client_order_id` is documented as a real idempotency key: a duplicate is rejected by the
  API, which is why retrying with the same value is safe. Pair it with
  `get_order_by_client_id` for reconciliation — no local id map needed beyond the `orders`
  table itself.

Gate structure selection on **`options_trading_level`** from the local `account` cache
(populated by `MarketPulseAgent`, sourced from `get_account_config` at connect and refreshed
each tick), never on `options_approved_level`: the former is the effective level, the
minimum of the approved level and the account's configured maximum.

### CLI (`src/options_m/cli.py`)

Argparse (no new dependency; the console script already exists in `pyproject.toml`).
Subcommands: `status`, `positions`, `chain --symbol SPY`,
`plan --symbol SPY --strategy call_debit_spread --delta 0.35 --dte 21` (builds and prints an
`OrderPlan` plus the risk verdict, never submits), and `trade --once` (runs one
`ExecutionAgent` iteration). `--json` on every subcommand. (`flatten` is added in Phase 4,
once `PositionManagerAgent` exists to enumerate what needs closing.)

This is a second, independent piece of evidence for hackathon rule #2 and costs almost
nothing because all the logic already lives in modules.

---

## Tests

- `test_strategy_builder.py` — see 2.5.
- `test_risk.py` — one test per rule in the table in 2.6, plus: kill switch blocks
  everything; NaN high-water mark does not disable the drawdown breaker; a naked short leg
  is always rejected; a symbol inside its `earnings.py` blackout window is rejected even when
  every other rule passes; a market-closed local-calendar row rejects with no MCP call made
  (assert on a mock that `get_clock` is never invoked).
- `test_execution.py` — dry run never calls a write tool; duplicate `client_order_id` is
  treated as success and reconciled; an MCP exception writes `status='failed'` and no fill; a
  rejected plan writes a `risk_event`; account/position reads inside `step()` never call
  `get_account_info`/`get_all_positions` directly (they go through the local cache).
- `test_evidence.py` — see 2.4.
- `test_store.py` — see 2.1.

---

## Acceptance criteria

- [ ] `options-m plan --symbol SPY --strategy call_debit_spread --delta 0.35 --dte 21`
      prints a plan with two **real** OCC symbols pulled from the live chain, a finite
      `max_loss`, and a risk verdict.
- [ ] The same command for `put_credit_spread` and `iron_condor` also produces valid,
      correctly-signed plans (negative `limit_price`).
- [ ] With `DRY_RUN=false` and a hand-inserted proposal, `ExecutionAgent` places a real
      multi-leg paper order that is visible in the Alpaca dashboard, and `orders` matches by
      `client_order_id`.
- [ ] Re-running the same proposal places no second order.
- [ ] Engaging the kill switch stops all submissions and records the event.
- [ ] A symbol seeded into `earnings.py`'s blackout window is rejected by `risk.py` with no
      order attempted.
- [ ] `market_calendar` is populated at startup and every agent's market-open check reads it
      locally — grep confirms no `get_clock(` call outside `market_pulse.py`.
- [ ] `ruff check . && mypy && pytest` green.

---

## Traps

- Market orders on options — always limit.
- Legging into a spread with two orders — one multi-leg request.
- Silent zero-quantity — must be an explicit rejection.
- A hardcoded spot-price fallback anywhere (`VibeHedge` uses `550.0`).
- Rejecting on a duplicate `client_order_id` as if it were an error. Alpaca's skill confirms
  the intended recovery: when a submission outcome is ambiguous and you have no order id,
  look the order up by your idempotency key with `get_order_by_client_id`.
- Building the order body from the REST schema instead of the tool schema.
- Sending anything but `time_in_force="day"` on an option order.
- Letting `risk.py` import anything from the agent/LLM layer.
- **Getting the credit/debit sign backwards** — negate `net_worst` exactly once, at the
  payload boundary, and unit-test the exact sign per structure family (see 2.5).
- Calling `get_clock`, `get_account_info`, or `get_all_positions` from anywhere other than
  their one owning agent — that defeats the entire point of the local-cache design.