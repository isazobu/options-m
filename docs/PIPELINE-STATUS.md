# Pipeline status — what runs end to end, and what does not

**Last verified:** 30 August 2026, by replaying the whole 24–28 August week
across the ten-symbol universe with `backtests/runs/2026-08-30_universe_agent-replay_1Day`
— 50 decisions through the real `EvidenceCollector` → `matrix.decide` →
`strategy_builder.build` → `RiskEngine.evaluate` chain. Earlier verification
was a single-symbol `options-m trace` against the replayed 28 August session.

This document exists because the plan docs describe the *intended* system and
`ARCHITECTURE.md` describes the *designed* one. Neither tells you which stages
actually carry a decision from evidence to order today. That is what this file
is for, and it should be updated whenever a stage changes state.

---

## How to see it for yourself

```bash
options-m trace --symbol SPY            # every stage, and which one stopped
options-m trace --symbol SPY --json     # same, machine-readable
options-m trace --symbol AAPL --fresh-evidence   # bypass MarketPulseAgent's cache
```

`trace` is read-only: it never writes a proposal and never places an order. It
calls the same helpers the agents call (`session.current`,
`fetch_chain_window`, `strategy_builder.build`, `RiskEngine.evaluate`), so it
cannot drift from what the running service does.

Out of hours, set `REPLAY_LAST_SESSION=true` first, or every trace stops at
stage 1. That flag is interlocked with `DRY_RUN` — startup refuses to run a
replayed session with order entry armed.

`trace` answers "where did *this* decision stop, right now". To ask what the
whole system would have done over a past stretch of days, use the replay
harness instead:

```bash
python backtests/runs/2026-08-30_universe_agent-replay_1Day/run.py
python backtests/runs/2026-08-30_universe_agent-replay_1Day/analyse.py
```

It runs the same shipped helpers against cached historical Alpaca data through
an as-of shim (`backtests/asof.py`) and a frozen clock (`backtests/clock.py`),
so like `trace` it cannot drift from the real pipeline. Gaps H, I, J and K
below were all found this way. Read that run's `notes.md` before quoting any
number from it — the bid/ask spread is modelled, not observed, because Alpaca
publishes no historical option quotes.

---

## The chain, stage by stage

Verified green for SPY on the replayed 28 Aug session:

| # | Stage | Where | Status |
|---|---|---|---|
| 1 | Session gate | `session.current` → `market_calendar` cache | ✅ |
| 2 | Kill switch + earnings blackout | `store`, `earnings.py` | ✅ |
| 3 | Evidence pack | `MarketPulseAgent` → `evidence` cache | ✅ |
| 4 | LLM regime read | `StrategistAgent` → Featherless, one call | ✅ |
| 5 | Strategy matrix | `matrix.decide` | ✅ |
| 6 | Spot price | `get_stock_snapshot` | ✅ |
| 7 | Chain fetch | `fetch_chain_window` | ✅ |
| 8 | Normalise + join by OCC symbol | `normalize_contracts` | ✅ |
| 9 | Contract selection + pricing | `strategy_builder.build` | ⚠️ all 9 built; wing snapping refuses valid strikes (gap I) |
| 10 | Risk gate | `risk.py` | ✅ |
| 11 | Submission | `ExecutionAgent` | ⏸ `DRY_RUN=true` by choice |
| 12 | Position management + exits | `PositionManagerAgent` | ❌ not implemented |
| 13 | Reflection → next decision | `ReflectionAgent` → `lessons` | ✅ written and read back into the evidence pack |

A real SPY trace reached stage 11: two real OCC contracts
(`SPY260925C00786000` / `SPY260925P00752000`), qty 2, limit 7.60, max loss
$1,520, risk gate approved.

Confirmed in the running service, not only in the trace: with the old build
every AAPL proposal was rejected `no_contracts_in_window` (proposals 6–11);
the first proposal on the fixed build (12) was rejected `wide_spread`. The
chain now reaches the target window and selects real contracts, and stops at a
genuine liquidity rule instead of an empty fetch.

---

## Fixed on 29 August (were silently killing every decision)

### 1. The contract fetch never reached the target DTE window

`ExecutionAgent` asked for `expiration_gte = today`, `expiration_lte = today +
dte_max`, with the default `limit=250`. Alpaca returns nearest expiry first,
so the entire budget went to 2–6 DTE contracts and the 21–38 DTE window the
builder then filtered for was always empty. Every proposal died as
`no_contracts_in_window`.

Fixed in `fetch_chain_window` (`agents/execution.py`): the window now starts at
`today + dte_min`, with a strike band around spot. `cli.py`'s `plan` had the
same bug and now shares the helper, so the manual path and the agent path
cannot drift apart again.

### 2. Delta was unrecoverable, so no contract could ever be selected

Alpaca ships `greeks` and `impliedVolatility` on the OPRA feed. On the feed a
paper account gets, both come back `None` for a meaningful share of contracts,
and `_effective_delta`'s Black-Scholes fallback needs an IV it did not have —
so it returned `None` for those and they dropped out of scoring.

`normalize_contracts` now solves sigma from the quote mid when the snapshot
carries none, exactly as `evidence.py` already did for its IV/RV read, and the
risk-free rate is threaded through so the vol we solve for and the delta we
compute from it come from one model.

### 3. The matrix and the builder spoke different languages

This was the dangerous one. `matrix.py` emits nine strategy names; the builder
implemented six *different* ones. The overlap was `long_call` and `long_put`
only:

| Matrix emits | Builder had | Result before the fix |
|---|---|---|
| `call_debit_spread` | `debit_call_spread` | same structure, different word order — fell through |
| `put_debit_spread` | `debit_put_spread` | same |
| `put_credit_spread` | — | fell through |
| `call_credit_spread` | — | fell through |
| `iron_condor` | — | fell through |
| `iron_butterfly` | — | fell through |
| `long_strangle` | — | fell through |

"Fell through" is not "was rejected". An unrecognised strategy is not in
`_CALL_STRATEGIES`, so `option_type` defaulted to `"put"`; it is not in
`_VERTICAL_STRATEGIES`, so no second leg was built; and `_risk_profile` fell
past every branch into the `cash_secured_put` arm. **A matrix decision for an
iron condor would have been built and priced as a single put with a
cash-secured put's risk profile.** Nothing on that path raised.

Three changes: the two renamed verticals are aliased to one canonical spelling;
anything genuinely unimplemented is now refused by name as
`unsupported_strategy`; and `long_strangle` gained a real builder.

---

## Gaps, in the order they block things

### A. All nine matrix structures now have a builder

**Fixed (30 August 2026, PR #13.)** `put_credit_spread`, `call_credit_spread`,
`iron_condor` and `iron_butterfly` are built, priced and risk-profiled. Every
cell of the matrix now resolves to a real structure:

|  | Premium expensive | Premium cheap |
|---|---|---|
| **Trend up** | ✅ put credit spread | ✅ call debit spread |
| **Trend flat** | ✅ iron condor / butterfly | ✅ long strangle |
| **Trend down** | ✅ call credit spread | ✅ put debit spread |

This only became reachable together with gap D. Before the ATM IV was measured
at the trading tenor, IV/RV sat at 0.45–0.62 for every symbol and the entire
expensive column was unreachable no matter how many builders existed. In the
24–28 August replay the matrix asked for the new structures **10 times**
(8 × `iron_condor`, 2 × `put_credit_spread`) and one — the NVDA iron condor —
was traded, for +$239 on $1,996 of risk. PR #13 and PR #14 only work together.

What remains on the selection path is gap I: the wing snapping refuses valid
strikes when the strike ladder is not uniform.

### B. `PositionManagerAgent` has no exit rules — nothing ever closes

It owns the positions cache and marks to market, and that is all. Profit
target, stop loss, DTE exit, time stop, thesis invalidation, and the priced
multi-leg closing order are all still Phase 4. A position opened today would be
held indefinitely. See `docs/plan/phase-4-position-reflection-dashboard.md` §1.

The 24–28 August replay puts a number on what that costs. The agent opened five
positions on Monday and **never traded again all week**:

| Day | Filled | Blocked by position limits | Other rejections |
|---|---:|---:|---:|
| Mon 24 Aug | 5 | 3 | 2 |
| Tue 25 Aug | 0 | 9 | 1 |
| Wed 26 Aug | 0 | 7 | 3 |
| Thu 27 Aug | 0 | 8 | 2 |
| Fri 28 Aug | 0 | 8 | 2 |

`MAX_CONCURRENT_POSITIONS = 5` filled on the first day and never freed a slot,
because nothing closes. **32 of the following 40 decisions were refused purely
because the book was full.** Without exits the system is not a trading agent
that runs continuously; it is a one-shot allocator that happens to keep
observing.

### C. The reflection loop — closed, with two loose ends

**The main claim here is fixed.** `evidence.py`'s `_lessons()` calls
`store.recent_lessons(symbol, 3)` and `store.recent_lessons(None, 2)`, so the
lessons `ReflectionAgent` writes land in the cached evidence pack that
`StrategistAgent` already reads. The system no longer writes memory it never
consults. Stage 13 in the table above is green.

Two related gaps are still open:

- **Pass B has no look-back window.** It reflects on proposals immediately
  rather than after 1–2 trading days, before the underlying has had time to
  prove the decision right or wrong.
- **Both passes read `orders`,** because the `trades` table they were specified
  against still does not exist. An order is not a fill, so the lesson is drawn
  from intent rather than outcome.

### D. The 250-row cap truncates the chain — and corrupts the volatility read

**Fixed (30 August 2026).** `get_option_chain` and `get_option_contracts` now
follow the server's `next_page_token` to the end of the band (page size 1000,
a 25-page ceiling that logs if it is ever hit), so a wide chain is read whole
instead of stopping at the first two expiries. And `evidence.py` no longer
reads ATM IV from `min(dte)`: it picks the expiry closest to the trading
tenor (`iv_dte_min..iv_dte_max`, default 21–38, wired from
`Settings.dte_target_*`), records it as `atm_expiry` / `atm_dte` in the pack,
and stores the IV-rank history keyed to that tenor. `iv_atm`, `iv_minus_rv`,
`put_call_skew` and the persisted history are now measured at the tenor the
structure is actually traded at. The description below is kept for context.

**Original finding.** Both `get_option_contracts` and `get_option_chain` were
capped at 250 rows, and both `evidence.py` and the builder hit that cap. In
the builder it merely narrows the choice set. In `evidence.py` it changes what
the system believes about volatility.

`EvidenceCollector` scans a 7–45 DTE window with a ±15% strike band. On SPY at
769 that band spans ~230 one-dollar strikes, so a *single* expiry already
exceeds 250 rows. What actually came back:

```
SPY   dte_window: [7, 45]        contracts_scanned: 250
      expiries_scanned: ["2026-09-08", "2026-09-09"]
      near_dte: 10   far_dte: 11
      iv_atm: 0.0925              <- 10-day implied vol
      realised_vol_20d: 0.1566    <- 20-day realised vol
```

Three consequences, in order of severity:

1. **IV/RV compares different tenors.** A 10-day ATM implied vol is divided by
   a 20-day realised vol. Short-dated IV is structurally low in a calm tape, so
   the ratio is biased down for every symbol:

   | | IV | RV | IV/RV |
   |---|---|---|---|
   | SPY | 0.0925 (10d) | 0.1566 (20d) | 0.59 |
   | AAPL | 0.237 (11d) | 0.3812 (20d) | 0.62 |
   | QQQ | 0.1395 (10d) | 0.3072 (20d) | 0.45 |

   The matrix's expensive column needs IV/RV ≥ 1.10. Nothing gets close, so
   **the entire short-premium half of the matrix is unreachable** and every
   proposal in the 29 Aug run came out `long_strangle`. Gap A is not the only
   reason those cells never fire — the signal never points at them either.

2. **The regime is measured at the wrong point on the curve.** The structure is
   chosen from 10-day implied vol, while the intent it produces asks for 21–38
   DTE contracts. The vol that picks the trade and the vol of the contracts
   actually traded are from different tenors.

3. **The term-structure signal is noise.** SPY's "far" expiry is 11 DTE — one
   day past near — and its ATM lookup failed outright (`iv_atm_far:
   NO_DATA_AVAILABLE`). QQQ recorded `term_structure: 0.1061` from two adjacent
   expiries (0.1395 → 0.2456), which is a thin-quote artefact, not a curve.

The truncation also varies by symbol without saying so: AAPL's $2.50 strike
spacing fits five expiries under the cap, so its far leg (34 DTE) is healthy,
while SPY's $1.00 spacing does not.

Fix, in order:

- ~~**Paginate on `next_page_token`**~~ — done. `get_option_chain` /
  `get_option_contracts` loop on the token; the band no longer has to be
  narrowed to fit under a cap.
- ~~**Pick the ATM expiry nearest the trading tenor**~~ — done.
  `evidence.collect(..., iv_dte_min, iv_dte_max)` selects the expiry closest to
  the `[iv_dte_min, iv_dte_max]` band (default 21–38) and reports it as
  `atm_dte` / `atm_expiry`.
- **Match the realised-vol window to the implied tenor.** Still open, but much
  smaller: `iv_atm` is now a ~30-day read against a 20-day RV, and `atm_dte` in
  the pack states the residual mismatch instead of leaving it implicit.

The credit structures (gap A) can now be added on top of a volatility read
taken at the tenor the matrix trades.

### E. Replayed sessions trip liquidity gates

Weekend quotes are last-session closing quotes, which are wider than anything
seen intraday. The AAPL trace was refused with `wide_spread` at 11.1% against a
10% limit — a correct rejection of stale data, not a bug. Expect replay runs to
die at the liquidity gate on all but the most liquid underlyings, and do not
loosen `MAX_SPREAD_PCT` to make a demo pass.

### F. `strategy_builder.py` had no tests at all

The module that turns an intent into real contracts and prices them — the one
place a mistake becomes a wrong order rather than a wrong log line — had no
test file. `tests/test_strategy_builder.py` now covers the refusal paths, the
alias, the IV fallback and the strangle (8 tests), but the six pre-existing
structures are still only covered indirectly. Any new structure from gap A
should arrive with its own pricing and max-loss tests.

### G. Still open from earlier review

- **The `config.py` default is fixed** — it now reads
  `account,trading,assets,options-data,stock-data`: the invalid `agents`
  toolset is gone and `trading` is present, so a deployment relying on the
  default does have `place_option_order`. What remains is only that `.env`'s
  `ALPACA_TOOLSETS` still carries `news`, which the 2026-08-29 design change
  removed. Harmless but stale.
- The paper account (`PA3WTOH8U1VV`, created 2025-01-02, equity $99,428.85) is
  not the brand-new $100,000 account the hackathon rules require.
- `/admin/kill` now exists: `GET` reports the switch, `POST` sets it, both
  behind the same bearer token as the dashboard routes. Engaging takes an
  optional note; **releasing requires a written reason**, because stopping a
  trading system should not be gated behind a form field and restarting one
  should not be a stray click. The response carries `env_forced` and
  `effective` as well as the stored flag — `ExecutionAgent` halts on
  `settings.kill_switch or store.is_kill_switch_engaged()`, so an operator who
  releases the stored flag while `KILL_SWITCH=true` needs to see that nothing
  actually resumed. Each change writes a `risk_events` row for the audit trail.
- Still missing: `options-m flatten` and the `trades` table.
- The seeded rows in `proposals` / `orders` / `risk_events` are demo data from
  `scripts/seed_demo_data.py`, stamped `mock: true`.

### H. The IV regime has no neutral band, so the system always trades

`matrix._iv_regime` classifies on one threshold: `IV/RV >= 1.10` is expensive
(sell premium) and **everything below it is "cheap"** (buy premium). There is
no band that means "fairly priced, do nothing".

Across the 24–28 August replay, `matrix.decide` returned `hold` **0 times in
50 decisions**. Most symbols sat at IV/RV 0.70–1.10 — roughly fair, not cheap —
and the matrix bought premium into all of it. `long_strangle` was selected 31
times out of 50.

That week's realised P&L, by structure family:

| Family | n | Wins | P&L | Return on risk |
|---|---:|---:|---:|---:|
| Debit vertical | 2 | 1 | +$1,889 | +56.4% |
| Short premium (iron condor) | 1 | 1 | +$239 | +12.0% |
| **Long premium (long strangle)** | **6** | **1** | **−$1,761** | **−19.2%** |

Six long strangles into a calm tape, five of them losers, bleeding theta while
the underlying went nowhere. One quiet week is not proof that long premium is
wrong — it *is* proof that a one-sided threshold will keep choosing it, 31
times out of 50, with no way to abstain.

Raising the position limits makes this worse rather than better, which is the
clearest evidence that the constraint is signal quality and not capital:

| `MAX_CONCURRENT_POSITIONS` | Filled | P&L | Win rate |
|---:|---:|---:|---:|
| 5 (current) | 5 | +$1,993 | 3/5 |
| 6 | 6 | +$1,647 | 3/6 |
| 10, with the 15% premium cap raised to 25% | 9 | +$367 | 3/9 |

The number of winners never moves off three. Every position added past the
first five lost money.

### I. Wing snapping refuses valid strikes on a non-uniform ladder

`strategy_builder.py`'s debit-vertical path computes its snap tolerance as the
**minimum** gap across the whole expiry:

```python
all_strikes = sorted({c.strike for c in candidates if c.expiry == primary.expiry})
increment = min(b - a for a, b in pairwise(all_strikes))
if abs(nearest - target_strike) > increment:   # -> no_strike_within_increment
```

Real ladders are not uniform. AMD's 2026-09-18 puts are spaced $2.50 near the
money (450–480) and **$10.00 out in the wing region** (390, 400, 410). The
minimum gap is 2.50, taken from the dense at-the-money region — but the wing is
snapped far out of the money, where the nearest listed strike can be up to
$5.00 from any target. A $2.50 tolerance rejects roughly half of all targets
there.

Observed: target 392.69, nearest listed 390.0, distance 2.69 > 2.50 →
`no_strike_within_increment`, structure refused. 390 was a perfectly good wing.
AMD lost two of five sessions to this in the replay.

This is not AMD-specific. It hits **any underlying whose ladder densifies near
the money** — most of them — and it hits precisely where every spread's
protective leg lives. The tolerance should come from the local gap bracketing
the target, not the global minimum.

### J. `_check_total_premium` adds two different quantities

`risk._check_total_premium` compares

```
portfolio.total_open_option_premium  +  plan.max_loss   <=  15% x equity
```

The left term is built by `build_portfolio_snapshot` as the sum of each *leg's*
absolute `market_value` — gross premium on both sides of every structure. The
right term added to it is a **max loss**. They are not the same quantity, and
for anything with a short leg they differ a lot.

The NVDA iron condor in the replay carried $3,137 of gross leg value against
$1,996 of actual risk: it consumed **57% more of the budget than it risked**.
The effect is backwards — the defined-risk credit structures the whole
short-premium thesis depends on are the ones penalised hardest by the cap.

Fix: measure the existing exposure the same way the new plan is measured, as
per-structure max loss rather than gross premium.

### K. Allocation is first-come-first-served in `UNIVERSE` order

There is no allocation flow. Sizing is one line —
`qty = floor(MAX_PREMIUM_PCT_PER_TRADE * equity / max_loss_per_contract)` —
and beyond that per-trade cap, slots go to whoever is evaluated first.
`ExecutionAgent` processes proposals one at a time, so the order that decides
the portfolio is the order symbols appear in `UNIVERSE`.

On 24 August the first five symbols that passed the gates took all five slots.
TSLA, META and GOOGL each produced a valid, fully-priced, risk-approved plan
and were refused `max_concurrent_positions` — not because they were worse, but
because they are later in the list.

Three things follow from the same root:

- **Conviction never reaches sizing.** The LLM produces a 0–1 conviction; it is
  used once as a binary veto at `CONVICTION_FLOOR` and then discarded. A 0.56
  and a 0.99 get identical size.
- **No correlation or concentration awareness.** The replay's five positions
  were SPY, QQQ, AAPL, MSFT and NVDA — one beta expressed five times, three of
  them the same long-vol structure. `MAX_POSITIONS_PER_UNDERLYING` cannot see
  this, because each ticker really is distinct.
- **`buying_power` is written but never read.** `store.py` records it for
  telemetry; no sizing or risk decision consults it, and nothing checks before
  submission that the account can actually carry the order. Everything sizes
  off `equity`.

### L. The clock is read from the stdlib, not passed in

`strategy_builder` (6 call sites), `risk`, `evidence.evidence` and
`agents.execution` all call `date.today()` / `datetime.now()` directly.
`matrix.decide` is the exception — it already takes `as_of`.

This is fine in a live service and blocks anything that has to evaluate the
pipeline at a past moment: a contract expiring 2026-09-18 is 24 DTE on the 25th
and 21 DTE on the 28th, so computing DTE against the real today silently shifts
every DTE filter, Black-Scholes delta and expected move. `backtests/clock.py`
works around it by swapping the module-level `date`/`datetime` names, which is
a workaround, not a design. Threading `as_of` through the way `matrix.decide`
already does would remove the need for it.

---

## Suggested order of work

1. ~~**Chain pagination + ATM tenor** (gap D)~~ — done (30 Aug 2026). The
   volatility read is now taken at the trading tenor, so the matrix can reach
   its short-premium cells.
2. ~~**Credit spreads and the two irons** (gap A)~~ — done. All nine matrix
   structures now have a builder.
3. ~~**Close the reflection loop** (gap C)~~ — done; `evidence.collect()` reads
   `recent_lessons`.
4. **Neutral IV band** (gap H) — the largest single lever. Until the matrix can
   answer "fairly priced, do nothing", it buys premium 31 times out of 50 and
   every other improvement just runs a poor signal more efficiently. Doing gap K
   first would only reorder bad picks.
5. **Wing snapping tolerance** (gap I) — small, isolated, and silently killing
   otherwise valid structures.
6. **Exit rules** (gap B) — without them nothing ever closes, which is the
   difference between a demo and a system. The replay makes the cost concrete:
   the book filled on Monday and stayed full for the rest of the week, refusing
   32 of the next 40 decisions on `max_concurrent_positions` alone.
7. **`_check_total_premium` units** (gap J) — small and isolated; unblocks the
   credit structures the cap currently over-charges.
8. **Allocation layer** (gap K) — batch scoring, conviction-weighted sizing and
   a concentration gate belong together, and belong after gap H.
9. `flatten` and the `trades` table (gap G); `as_of` threading (gap L).
   `/admin/kill` is done.

Gaps H, I, J and K were all found by replaying a full week through the real
pipeline. That harness lives in `backtests/` and is the cheapest way to check
whether any of the above actually changed behaviour: fix, re-run the same week,
compare.
