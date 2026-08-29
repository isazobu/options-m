# Phase 2 — Evidence, strategy construction, risk engine, execution

**Master doc:** `hackathonda-in-a-etmen-istenen-shiny-clarke.md`
**Prerequisite:** Phase 1 complete — `AlpacaMcp` session live, `Store` writing to Postgres,
`MarketPulseAgent` running, service deployed with `DRY_RUN=true`.
**Goal:** produce a fully-formed, risk-approved **options order plan** from real chain data
and submit it — still no LLM. At the end of this phase, a hand-written `StrategyIntent`
goes in and a real paper options order comes out.

This phase is where the hackathon's "options" requirement is actually satisfied, and where
all the safety credibility lives. Build it before the LLM, not after.

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

### 1. Domain types — `src/options_m/models.py`

Pydantic models, strict. These are the contracts every later phase speaks.

```python
class StrategyIntent(BaseModel):
    """What the LLM is allowed to decide. It never names a contract."""
    action: Literal["open", "hold", "close"]
    strategy: Literal["long_call", "long_put", "debit_call_spread",
                      "debit_put_spread", "covered_call", "cash_secured_put"]
    underlying: str
    target_delta: float          # e.g. 0.35 for the long leg
    spread_width: float | None   # for verticals, in strike points
    dte_min: int
    dte_max: int
    conviction: float            # 0..1
    thesis: str
    invalidation: str            # what would prove this wrong

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
    limit_price: float           # net debit/credit per contract
    max_loss: float              # total dollars at risk, always finite
    max_profit: float | None
    breakeven: float | None
    client_order_id: str         # f"om-{proposal_id}" — idempotency key
```

`max_loss` being non-`None` and finite is the gate for "defined risk". If it cannot be
computed, the plan is rejected.

### 2. `src/options_m/evidence.py`

Deterministic evidence collection for one underlying. No LLM. Returns a compact dict that
serialises to well under the model's context budget (target ≤ 4 KB of JSON).

Collect via `AlpacaMcp`:
- `get_stock_snapshot(symbol)` — last trade, daily bar, prev close
- `get_stock_bars(symbol, timeframe="1Day", limit=60)` — compute SMA20/SMA50, RSI14,
  ATR14, 20-day realised volatility, distance from 52-week high/low. Compute these
  **ourselves** from the bars; do not add a `stockstats`/`pandas` dependency for it.
- `get_option_chain(symbol, ...)` filtered to the DTE window → summarise into
  `iv_atm`, `iv_rank` (vs the last N snapshots we stored), `put_call_skew`,
  `term_structure` (near vs far ATM IV), `median_spread_pct`, `total_open_interest`.
  The math already exists: `options_m.volatility` has `iv_rank` / `iv_percentile`
  and BSM `implied_vol` (invert a mid quote when the chain carries no IV), and
  `Store.iv_rank_for(symbol)` / `Store.recent_iv(symbol)` read the history below.
- `get_news(symbols=[symbol], limit=5)` — headline + summary only, truncated. The MCP
  server classifies this tool's output as `external_text` in its trust-boundary envelope
  (`../alpaca-mcp-server/src/alpaca_mcp_server/security.py`): it is attacker-influenceable
  text. Store it in the evidence pack under a clearly named `untrusted_news` key and let
  Phase 3 fence it inside the prompt — never splice raw headlines into an instruction.
- `store.recent_lessons(symbol, n)` — Phase 4 fills this; return `[]` for now
- current position in this underlying, if any

**Missing-data discipline:** any field we could not fetch becomes the literal string
`"NO_DATA_AVAILABLE"`, and the evidence pack carries a top-level note:
`"Fields marked NO_DATA_AVAILABLE are genuinely unavailable. Do not estimate or fabricate values."`
Borrowed from `TradingAgents/tradingagents/dataflows/interface.py:242`. This is what stops
a model from inventing an IV number.

Persist every pack into `proposals.evidence` (JSONB) so a judge can replay the decision.

Store an `iv_history` row per symbol per day so `iv_rank` becomes meaningful within a day
or two of running — start writing it in this phase. The table
(`iv_history(id, ts, symbol, iv_atm, dte, spot, payload)`) and
`Store.append_iv_snapshot(...)` already exist; this phase adds the writer (a per-symbol
ATM-IV reading from the chain, once per pull).

#### Pack shape (as built)

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
untrusted_news    list[dict] | "NO_DATA_AVAILABLE"          (≤ 5 items)
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

`trend` — computed here from ~252 daily bars (not 60; needed for a real 52-week range and a
defined SMA50):

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

`position` — `get_all_positions` filtered to this underlying (equity by ticker, option legs
by parsed OCC underlying):

```
option leg:  kind="option", symbol, option_type, strike, expiry,
             side, qty, avg_entry_price, market_value, unrealized_pl, unrealized_plpc
equity:      kind="equity", symbol, side, qty, avg_entry_price,
             market_value, unrealized_pl, unrealized_plpc
```

`untrusted_news` — from `get_news`, headline + summary only:

```
headline      truncated ≤ 200 chars
summary       truncated ≤ 320 chars
source
created_at
```

`collect()` only reads and returns — it does not persist. Writing the pack into
`proposals.evidence` is done by whichever component owns the `proposals` row (the Phase 3
strategist, or `ExecutionAgent`), so the `proposals` table and its `Store` methods land with
that work, not here. The one write `collect()` does make is the IV-history row
(`append_iv_snapshot`), once per pull, right before the rank is read.

### 3. `src/options_m/strategy_builder.py`

`async def build(intent: StrategyIntent, chain, account) -> OrderPlan | Rejection`

The core anti-hallucination component. Steps:

1. Filter the live chain to `intent.underlying`, the right `option_type`, and expiries
   inside `[dte_min, dte_max]`. Prefer standard monthly expiries when several qualify.
2. **Select the long leg** as the contract whose absolute delta is closest to
   `intent.target_delta`. If the chain snapshot carries no greeks, compute delta with
   Black-Scholes from mid price, spot, strike, DTE and implied vol — use
   `options_m.volatility.bsm_greeks` (and `implied_vol` to recover sigma from the mid).
   Use the chain's greeks when present; BS is the fallback, and the plan records which was used.
3. **Verticals:** pick the short leg at `long_strike ± spread_width`, same expiry, snapping
   to the nearest listed strike. Reject if no listed strike is within one strike increment.
4. **Liquidity checks** (reject with a reason, do not "fix"): bid/ask present and > 0,
   `spread_pct = (ask-bid)/mid` under the configured cap, open interest above the minimum.
5. **Pricing:** limit price = mid of the net structure, then nudged by a configurable
   fraction of the spread toward the aggressive side. **Never a market order** — options
   spreads are wide and `VibeHedge` sends market orders on every path.
6. **Compute `max_loss` analytically per structure.** Long single: debit paid. Debit
   vertical: net debit. Covered call: assumes the shares exist — verify with
   `get_open_position` first. Cash-secured put: strike × 100 × qty minus credit. If a
   structure would ever be undefined-risk, return a `Rejection`.
7. **Size:** `qty = floor(max_premium_budget / max_loss_per_contract)`, then clamp by the
   position-count and per-underlying caps. `qty == 0` is a `Rejection`, not a silent no-op —
   `AlpacaTradingAgent`'s `int(amount/price)` silently placing nothing is exactly the bug
   we do not want.
8. `client_order_id = f"om-{proposal_id}"`.

### 4. `src/options_m/risk.py` — the risk engine

Zero LLM, zero MCP writes, no imports from `crew.py`. It takes an `OrderPlan` plus an
account/portfolio snapshot and returns `RiskVerdict(approved: bool, reasons: list[str],
adjusted_qty: int | None)`. Modelled on
`AlpacaTradingAgent/tradingagents/safety/guardrails.py`, which deliberately has no agent
imports so it can never be reasoned around.

Rules, each individually configurable and each individually unit-tested:

| Rule | Default |
| --- | --- |
| Max premium at risk per trade | 2% of equity |
| Max total open option premium | 15% of equity |
| Max concurrent option positions | 5 |
| Max positions per underlying | 1 |
| Defined risk only — reject any naked short leg | always on |
| DTE window | 7–45 |
| Min open interest per leg | 100 |
| Max bid-ask spread | 10% of mid |
| Daily loss halt | −3% of start-of-day equity |
| Drawdown halt vs high-water mark | −8% |
| Market must be open (`get_clock`) | on |
| Minutes-before-close blackout | 15 |
| Kill switch (env flag OR `kill_switch` table) | checked every call |
| Idempotency: no existing order with this `client_order_id` | always on |

Every rejection is written to `risk_events(ts, proposal_id, rule, detail jsonb)`. This table
is prime dashboard material — "the agent declined 14 trades and why" is a stronger judging
story than the trades it took.

Numeric hygiene: reuse the `_finite_float` semantics — a NaN high-water mark must not
permanently disable the drawdown breaker.

### 5. `src/options_m/trading/execution.py` — `ExecutionAgent`

Cadence 30 s. No LLM.

```
step():
  1. if kill switch engaged: log, return
  2. proposals = store.pending_proposals(limit=5)
  3. for each:
       plan = strategy_builder.build(intent, chain, account)
       if Rejection: mark proposal 'rejected', write risk_event, continue
       verdict = risk.evaluate(plan, portfolio)
       if not approved: mark 'rejected', write risk_events, continue
       if settings.dry_run:
           mark 'dry_run_approved', persist the full plan, continue
       order = await mcp.place_option_order(...)   # legs, qty, limit, DAY, client_order_id
       store.record_order(plan, order, status='submitted')
  4. reconcile: for orders in flight, get_order_by_id -> update fills/status
```

**Order submission rules:**
- Always pass `client_order_id`. If Alpaca rejects it as a duplicate, that is a *success*
  path — the order already exists; reconcile instead of retrying.
- On any exception: `orders.status='failed'` with the error text. **Never** synthesise a
  fill. (`VibeHedge/src/execution/alpaca_trader.py:196-219` returns `FILLED_SIMULATED` with
  a hardcoded $3.50 premium — the single most dangerous pattern in the reference set.)
- Read the current position with a strict path before deciding: a broker outage must raise,
  not read as "flat". (`AlpacaTradingAgent/.../alpaca_utils.py:694-768` — the best comment
  in that repo.)
- Multi-leg goes through `place_option_order` with both legs in one request, so the spread
  fills as one unit; never leg into a spread with two separate orders.

**`place_option_order` — exact contract** (read from
`../alpaca-mcp-server/src/alpaca_mcp_server/overrides.py:258-340`; this tool is a hand-written
override, so its schema is *not* in `tool_registry.py` and not in the OpenAPI spec):

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
- **A vertical is one order:** `qty="1"`, `legs=[{"symbol": long, "ratio_qty": "1", "side":
  "buy", "position_intent": "buy_to_open"}, {"symbol": short, "ratio_qty": "1", "side":
  "sell", "position_intent": "sell_to_open"}]`, `limit_price` = net debit as a **positive**
  number, and no parent `symbol`/`side`. Do not set `order_class` — it is inferred.
- Always pass `position_intent`. It is optional in the API but it is what makes a closing
  order unambiguous, and it is cheap insurance against accidentally opening a short leg.
- The override validates locally and returns an `{"error": ...}` dict (it does **not** raise)
  when a single-leg call is missing `symbol`/`side` or a multi-leg call is missing `legs`.
  So after every write call, check for an `error` key in the unwrapped payload and treat it
  as a failure — a returned dict is not proof of a submitted order.
- `client_order_id` is documented as a real idempotency key: a duplicate is rejected by the
  API, which is why retrying with the same value is safe. Pair it with
  `get_order_by_client_id` (see the Phase 1 tool list) for reconciliation — no local id map
  needed.

### 5b. The `place_option_order` contract (verified against the server source)

Do **not** build parameters from Alpaca's REST schema. The skill warns that the `place_*`
tools deliberately reshape the REST body and set `additionalProperties: false`, so a nested
REST-shaped payload is a hard rejection, not an ignored field. From
`../alpaca-mcp-server/src/alpaca_mcp_server/overrides.py`:

```python
place_option_order(
    qty: str,                       # REQUIRED. For multi-leg this is the strategy
                                    # multiplier: each leg's ratio_qty is scaled by it
    type: str = "market",           # "market" | "limit" — we always send "limit"
    time_in_force: str = "day",     # "day" ONLY; options support nothing else
    symbol: str | None = None,      # OCC symbol, e.g. "AAPL250321C00150000". Single-leg only
    side: str | None = None,        # "buy" | "sell". Single-leg only
    position_intent: str | None = None,   # buy_to_open | buy_to_close |
                                          # sell_to_open | sell_to_close — always send it
    limit_price: str | None = None, # net debit POSITIVE, net credit NEGATIVE
    client_order_id: str | None = None,   # idempotency key; the API rejects duplicates
    order_class: str | None = None, # "mleg"; auto-inferred when legs is present
    legs: list[dict] | None = None, # max 4; each {symbol, ratio_qty(str), side,
                                    # position_intent}
)
```

Notes that will bite otherwise: **every numeric is a string**; single-leg requires
`symbol` + `side` while multi-leg requires `legs` and ignores both; `limit_price` sign encodes
debit vs credit for spreads. Always pass `position_intent` — it is what distinguishes opening
a position from closing one, and Phase 4's exits depend on it.

Gate structure selection on **`options_trading_level`** from `get_account_info` (already read
at connect in Phase 1 and exposed on `/api/status`), never on `options_approved_level`: the
former is the effective level, the minimum of the approved level and the account's configured
maximum.

### 6. Schema additions

`proposals(id, ts, underlying, status, intent jsonb, evidence jsonb, arguments jsonb,
verdict jsonb, plan jsonb, error text)` — `arguments`/`verdict` stay null until Phase 3.
`orders(id, proposal_id, client_order_id unique, submitted_at, status, request jsonb,
response jsonb, filled_qty, filled_avg_price, error text)`.
`risk_events(id, ts, proposal_id, rule, detail jsonb)`.
`iv_history(id, ts, symbol, iv_atm numeric, ...)`.

### 7. CLI (`src/options_m/cli.py`)

Argparse (no new dependency; the console script already exists in `pyproject.toml`).
Subcommands: `status`, `positions`, `chain --symbol SPY`,
`plan --symbol SPY --strategy debit_call_spread --delta 0.35 --dte 21` (builds and prints an
`OrderPlan` plus the risk verdict, never submits), and `trade --once` (runs one
`ExecutionAgent` iteration). `--json` on every subcommand.

This is a second, independent piece of evidence for hackathon rule #2 and costs almost
nothing because all the logic already lives in modules.

---

## Tests

- `test_strategy_builder.py` — with a fixture chain: delta selection picks the right strike;
  vertical width snapping; rejection when no strike is within one increment; rejection on a
  wide spread / low open interest; `max_loss` correct for each of the six structures; BS
  fallback engages when greeks are absent; `qty == 0` returns a rejection rather than a plan.
- `test_risk.py` — one test per rule, plus: kill switch blocks everything; NaN high-water
  mark does not disable the drawdown breaker; a naked short leg is always rejected.
- `test_execution.py` — dry run never calls a write tool; duplicate `client_order_id` is
  treated as success and reconciled; an MCP exception writes `status='failed'` and no fill;
  a rejected plan writes a `risk_event`.
- `test_evidence.py` — a failed sub-fetch yields `NO_DATA_AVAILABLE`, never a made-up number.

---

## Acceptance criteria

- [ ] `options-m plan --symbol SPY --strategy debit_call_spread --delta 0.35 --dte 21`
      prints a plan with two **real** OCC symbols pulled from the live chain, a finite
      `max_loss`, and a risk verdict.
- [ ] With `DRY_RUN=false` and a hand-inserted proposal, `ExecutionAgent` places a real
      multi-leg paper order that is visible in the Alpaca dashboard, and `orders` matches by
      `client_order_id`.
- [ ] Re-running the same proposal places no second order.
- [ ] Engaging the kill switch stops all submissions and records the event.
- [ ] `ruff check . && mypy && pytest` green.

---

## Traps

- Market orders on options — always limit.
- Legging into a spread with two orders — one multi-leg request.
- Silent zero-quantity — must be an explicit rejection.
- A hardcoded spot-price fallback anywhere (`VibeHedge` uses `550.0`).
- Rejecting on a duplicate `client_order_id` as if it were an error. Alpaca's skill confirms
  the intended recovery: when a submission outcome is ambiguous and you have no order id, look
  the order up by your idempotency key with `get_order_by_client_id`.
- Building the order body from the REST schema instead of the tool schema (see 5b).
- Sending anything but `time_in_force="day"` on an option order.
- Letting `risk.py` import anything from the agent/LLM layer.
