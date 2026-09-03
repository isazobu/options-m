# Backtesting Guide

## Approach: Pipeline Replay

options-m's backtest method is **pipeline replay** — instead of rewriting the
strategy from scratch, it runs the production code itself against historical
data.

```
run.py
  │
  ├── AsOfMcp (backtests/asof.py)
  │     Implements AlpacaMcp's read interface.
  │     Returns cached data from Alpaca's historical API instead of live MCP
  │     calls — EvidenceCollector and strategy_builder have no idea.
  │
  ├── frozen_at (backtests/clock.py)
  │     Pins every date.today() / datetime.now() call inside strategy_builder,
  │     risk, evidence, execution, and strategist to the replay date.
  │
  └── Production code that runs unchanged:
        EvidenceCollector.collect()
        matrix.decide()
        fetch_chain_window()
        strategy_builder.build()
        RiskEngine.evaluate()
```

**Why this matters:** a backtest that rewrites the pipeline measures the
rewrite. What's measured here is the actual production decision.

---

## Quick Start

Each run directory's `raw/` folder (historical stock bars, option contracts,
and option bars) is gitignored — it's ~13MB of cached Alpaca CLI output,
re-fetchable from the parameters and SHA-256 digests recorded in that run's
`data_fingerprint.json`. Re-fetch it with the same `alpaca` CLI and parameters
before running an existing replay on a fresh clone.

```bash
# Results are written as JSON; analyse.py reads them and prints a table
python backtests/runs/2026-08-30_universe_agent-replay_1Day/run.py
python backtests/runs/2026-08-30_universe_agent-replay_1Day/analyse.py
```

Once `raw/` is populated, `run.py` makes no network calls at all — `AsOfMcp`
reads straight from the cached JSON files.

---

## Running a New Replay

### Layout

```
backtests/runs/<YYYY-MM-DD>_<description>/
├── run.py          # fetch data + run pipeline + save results
├── analyse.py      # read saved results + print report
└── notes.md        # assumptions, deviations, observations
```

### Minimum structure for run.py

```python
from datetime import date
from pathlib import Path
from backtests.asof import AsOfMcp, StubStore
from backtests.clock import frozen_at
from options_m.evidence.evidence import EvidenceCollector
from options_m.matrix import decide
from options_m import strategy_builder
from options_m.risk import RiskEngine
from options_m.config import Settings
from options_m.models import RegimeRead

RAW_DIR = Path(__file__).resolve().parent / "raw"  # populated ahead of time
REPLAY_DATES = [date(2026, 8, 25), date(2026, 8, 26), ...]
SPREAD_PCT = 0.02  # modelled bid/ask half-spread
SETTINGS = Settings()
STORE = StubStore()

for replay_date in REPLAY_DATES:
    mcp = AsOfMcp(RAW_DIR, as_of=replay_date, spread_pct=SPREAD_PCT)

    with frozen_at(replay_date):
        evidence = EvidenceCollector(SETTINGS, mcp, STORE)
        for symbol in SETTINGS.universe_symbols:
            pack = await evidence.collect(symbol, ...)
            regime = RegimeRead(thesis="...", invalidation="...", conviction=0.70)  # LLM stub
            decision = decide(pack, regime, settings=SETTINGS, as_of=replay_date)
            if hasattr(decision, 'strategy'):
                # strategy_builder + RiskEngine + persist...
```

### Critical rules

- `AsOfMcp` reads synchronously from the cached JSON files in `raw/` at
  construction time — it makes no network calls itself, so `raw/` must
  already be populated before the replay starts.
- The `frozen_at` context manager must be active while strategy and risk
  code runs.
- The LLM call must either be stubbed (with an off-production conviction) or
  actually executed. Stubbing gives repeatability and measures an upper bound
  on production results.
- `AsOfMcp` implements only read methods: there is no `place_option_order`.

---

## Data Constraints and Their Effects

These constraints come from the nature of the data source — they cannot be
fixed.

### 1. No historical option quotes — spread must be modelled

Alpaca provides historical option **bar** and **trade** data, but no
historical **quote** data. `AsOfMcp` does the following:

```
mid  = that day's option bar close
bid  = mid × (1 − spread_pct)
ask  = mid × (1 + spread_pct)
```

Both are rounded to the real tick grid ($0.01 below $3.00, $0.05 above).

**Practical effect:** the `MAX_SPREAD_PCT` gate filters on an assumption. For
that reason, runs **sweep** `spread_pct` rather than using a single fixed
value:

| `spread_pct` | Trades made | P&L |
|---|---|---|
| 0.5% | 5 | +$2,057 |
| 2% (headline) | 5 | +$1,993 |
| 5% | 5 | +$1,755 |
| 10% | 5 | +$404 |

If the table is stable across 0.5%–5%, the result is reliable.

### 2. IV and delta are modelled, not observed

Historical snapshots carry no IV or greeks — neither does the production
feed. Both come back as `None`, and the project's own Black-Scholes solver
takes over. This is the same production code path, not a backtest-only
shortcut.

### 3. Open interest is not point-in-time (slight look-ahead)

`open_interest` comes from Alpaca's contracts endpoint and carries a single
`open_interest_date` stamp (a few days stale). The `OI` filter for today
includes a few days of look-ahead. Because open interest moves slowly the
distortion is small, but it is real and cannot be removed.

### 4. A contract with no trades that day is absent from that day's chain

11,888 of 15,684 contracts carry a bar within the window. No trade on a given
day means no quote source; this is both a raw liquidity filter and a bias in
the selectable set toward liquid strikes.

### 5. Daily granularity

A run evaluates once per day. Intraday entries, intraday gate changes, and
same-day exits cannot be represented with daily bars.

---

## LLM Backtesting Options

### Option A — Stub it (recommended)

```python
regime = RegimeRead(thesis="stubbed", invalidation="stubbed", conviction=0.70)
```

- **Pro:** fully repeatable; no network calls; measures an upper bound.
- **Con:** the conviction veto is bypassed; the 0.70 choice affects results.
- **When to use:** when testing matrix logic, strategy construction, or risk
  limits.

### Option B — Real LLM

Call the actual `StrategistAgent._run()` to produce `RegimeRead` (LLM
enabled, with an API key).

- **Pro:** conviction distribution is realistic; the system is tested more
  holistically.
- **Con:** token cost; different results across runs; hallucination risk from
  the LLM's assumption of "today".
- **When to use:** for full end-to-end validation, logging the outputs.

---

## How to Run the Most Effective Backtest

### Minimum bar for reliable findings

| Requirement | Why |
|---|---|
| **≥ 50 decisions** | For statistical meaning — a handful of trades is not a "win rate", it's noise. |
| **More than one market regime** | Trend-only or range-only isn't enough; both are needed. |
| **Sweep `spread_pct`** | Shows whether the spread assumption changes the results. |
| **Fixed LLM conviction** | Isolates sources of uncertainty; test determinism first. |

### Parameter sensitivity

Run separate replays per variable when changing configuration values. The
following are the most effective levers:

```python
# Matrix signal quality
CONVICTION_FLOOR          # lower -> more trades, noisier signal
# Not well tested yet: no IV-neutral band (see Constraints)

# Risk limits
MAX_CONCURRENT_POSITIONS  # above 5 = bad days become unavoidable
MAX_PREMIUM_PCT_PER_TRADE # sizing effect
DAILY_LOSS_HALT_PCT       # how aggressively trading halts

# Structure parameters
DTE_TARGET_MIN/MAX        # currently 7-14; what shorter/longer changes
SHORT_DELTA_DEFAULT       # currently 0.25; 0.20 vs 0.30
```

### Walk-forward testing

Optimizing for a single date range overfits. Instead:

```
Training window: [date_A, date_B]  ← parameter selection
Test window:     [date_B, date_C]  ← evaluation without having seen it in training
```

The current harness supports this naturally — run two separate replays with
different date lists.

### Exit logic

The `2026-08-30_universe_agent-replay_1Day` run marks positions at the close
and tests only **entry decisions**, not exits — profits there are understated
(the closing half-spread is not deducted) and holding-period realism is
limited.

The `exit-flow` and `exit-rules-v2` runs extend the harness to also replay the
five-rung exit ladder in `options_m/exits.py` (expiry, DTE stop, stop-loss,
profit target, time stop) day by day, so a proposed close can actually fill on
the next session's open. See those runs' `notes.md` for the caveats specific
to exit replay (n=1 per week, no historical option quotes, Friday closes never
get a fill day in the window).

---

## Interpreting Results

### Always report these

| Metric | Why |
|---|---|
| Number of decisions | Bounds statistical confidence per observation. |
| `spread_pct` sweep table | Shows the spread assumption's effect. |
| Structure family breakdown | Which strategy types were chosen. |
| Rejected → why | Which gates fired. |
| Look-ahead disclosure | Both the OI and spread model. |

### What not to trust

- Exact P&L figures (spread model ± commissions).
- Sharpe ratio (from a handful of daily-granularity observations).
- "This parameter is better" conclusions from a single one-week window.

### What to trust

- Which gates fired, and how often.
- How many trades a given configuration makes.
- Which rejection reasons dominate (defines poor signal quality).
- If `spread_pct` is stable across 0.5%–5%, the spread model is not dominant.

---

## Current Open Backtest Questions

These can be answered directly on the harness:

1. **IV-neutral band effect** — how much does adding a "hold" zone for
   decisions where IV/RV sits between 0.85–1.10 reduce `long_strangle`
   selection?

2. **Wing-snap tolerance** — if the local minimum gap is corrected to a
   global one instead, how many additional structures can be built? (AMD hit
   this weekly.)

3. **Conviction threshold calibration** — is 0.55 right, or should it be
   higher? Measure by sweeping a real LLM conviction distribution over a
   range, not a fixed stub.

4. **Exit rule tuning** — now that `exit-flow`/`exit-rules-v2` replay the exit
   ladder, sweep `EXIT_CREDIT_PROFIT_TARGET_PCT` / `EXIT_CREDIT_STOP_LOSS_PCT`
   / `EXIT_TIME_STOP_DAYS` against a longer window to see which rung dominates
   outcomes.

---

## Important Disclosure

> This backtest is a hypothetical historical simulation and does not
> represent actual trading performance. Past results do not guarantee future
> results. Results depend on market data quality, data feed selection,
> corporate action handling, fees, slippage, liquidity, taxes, execution
> assumptions, and implementation details. This material is for research and
> educational purposes only.
