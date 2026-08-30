# Pipeline status — what runs end to end, and what does not

**Last verified:** 29 August 2026, by tracing SPY and AAPL with
`options-m trace` against the replayed 28 August session.

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
| 9 | Contract selection + pricing | `strategy_builder.build` | ⚠️ 5 of 9 structures missing |
| 10 | Risk gate | `risk.py` | ✅ |
| 11 | Submission | `ExecutionAgent` | ⏸ `DRY_RUN=true` by choice |
| 12 | Position management + exits | `PositionManagerAgent` | ❌ not implemented |
| 13 | Reflection → next decision | `ReflectionAgent` → `lessons` | ⚠️ writes, never read back |

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

### A. Four structures still have no builder — blocks 4 of 9 matrix cells

`put_credit_spread`, `call_credit_spread`, `iron_condor`, `iron_butterfly` are
refused as `unsupported_strategy`. The matrix reaches for them whenever premium
is expensive (IV/RV ≥ 1.10), which is precisely the regime the whole
short-premium thesis is built around. Today the system can only act on the
*cheap*-premium half of the matrix:

|  | Premium expensive | Premium cheap |
|---|---|---|
| **Trend up** | ❌ put credit spread | ✅ call debit spread |
| **Flat** | ❌ iron condor / butterfly | ✅ long strangle |
| **Trend down** | ❌ call credit spread | ✅ put debit spread |

Each needs leg selection, the credit/debit sign convention (Alpaca's multi-leg
`limit_price` is positive for a debit, negative for a credit), a max-loss that
is width − credit rather than premium paid, and the credit ≥ 12% of width floor
from `00-MASTER.md`. The two credit spreads are the cheapest to add and unlock
four of the six missing cells; the two iron structures are 4-leg and share most
of that machinery once it exists.

### B. `PositionManagerAgent` has no exit rules — nothing ever closes

It owns the positions cache and marks to market, and that is all. Profit
target, stop loss, DTE exit, time stop, thesis invalidation, and the priced
multi-leg closing order are all still Phase 4. A position opened today would be
held indefinitely. See `docs/plan/phase-4-position-reflection-dashboard.md` §1.

### C. The reflection loop is open, not closed

`ReflectionAgent` writes lessons and `store.recent_lessons()` exists, but
nothing calls it. Per the Phase 4 design the read belongs in
`MarketPulseAgent`'s `evidence.collect()`, so the lesson lands in the cached
evidence pack that `StrategistAgent` already reads — no change needed on the
strategist side. Until then the system writes memory it never consults.

Related: Pass B has no look-back window, so it reflects on proposals
immediately rather than after 1–2 trading days, before the underlying has had
time to prove the decision right or wrong. And both passes read `orders`
because the `trades` table they were specified against does not exist yet.

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

- `FEATHERLESS_MODEL_DEEP` is now set, but `.env`'s `ALPACA_TOOLSETS` still
  carries `news`, which the 2026-08-29 design change removed. Worse, the
  default in `config.py` is `account,agents,assets,options-data,stock-data` —
  `agents` is not a valid toolset, and `trading` is missing, so a deployment
  that relies on the default has no `place_option_order` at all.
- The paper account (`PA3WTOH8U1VV`, created 2025-01-02, equity $99,428.85) is
  not the brand-new $100,000 account the hackathon rules require.
- No `options-m flatten`, no `trades` table, no `/admin/kill` endpoint.
- The seeded rows in `proposals` / `orders` / `risk_events` are demo data from
  `scripts/seed_demo_data.py`, stamped `mock: true`.

---

## Suggested order of work

1. ~~**Chain pagination + ATM tenor** (gap D)~~ — done (30 Aug 2026). The
   volatility read is now taken at the trading tenor, so the matrix can reach
   its short-premium cells.
2. **Credit spreads** (gap A, first half) — unlocks four matrix cells and the
   short-premium thesis the project is built on. Now unblocked by D.
3. **Exit rules** (gap B) — without them nothing ever closes, which is the
   difference between a demo and a system.
4. **Close the reflection loop** (gap C) — one call in `evidence.collect()`.
5. **Iron condor / butterfly** (gap A, second half) — reuse the credit-spread
   machinery.
6. `flatten`, `trades`, `/admin/kill` (gap G).
