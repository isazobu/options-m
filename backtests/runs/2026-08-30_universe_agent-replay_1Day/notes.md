# Replay run — options-m autonomous agent, 24–28 August 2026

## The request

> "If our autonomous agent had been placing trades last week, what results would
> it have produced?"

## What this run actually is

A **replay of the real decision pipeline against historical data**, not a
strategy backtest written from scratch. `run.py` imports and calls the shipped
code — `EvidenceCollector.collect`, `matrix.decide`, `fetch_chain_window`,
`strategy_builder.build`, `RiskEngine.evaluate` — and supplies only four things
the live system takes from the world instead of from an argument:

| Supplied by the harness | How |
|---|---|
| The clock | `backtests/clock.py` freezes `date.today()` / `datetime.now()` per replay day |
| Market data | `backtests/asof.py` serves cached Alpaca history through the `AlpacaMcp` read surface |
| The LLM regime read | Stubbed at fixed conviction (see below) |
| Fills and marks | Modelled here; the live system has no exit logic yet |

Anything the pipeline decides, it decided on its own.

## Code under test

`options-m` `main` at commit `162aeae` (PR #14, "measure ATM IV at the trading
tenor, and page the option chain", merged on top of PR #13, the four credit
structures). 283 unit tests green at that commit.

This matters: on the previous `main` the ATM IV was read at the nearest expiry
(10–11 DTE) and divided by 20-day realised vol, so IV/RV sat around 0.45–0.62
for every symbol and the matrix's expensive column — everything short-premium —
was unreachable. Replaying *that* build would have produced `long_strangle` and
nothing else. The IV fix is what makes this run informative.

## Confirmed interpretation

- **Universe:** SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMD, TSLA, META, GOOGL (`.env` `UNIVERSE`)
- **Signal timing:** completed daily bar close on day D. Evidence sees stock bars
  and option closes up to and including D, never past it.
- **Fill timing / model:** `next_open` — day D+1's option bar open, per leg,
  crossing the modelled spread in the direction that costs us (buy at ask, sell
  at bid). A signal on Friday 28th has no fill day and is recorded as such.
- **Mark:** Friday 28 August close, at mid, per leg.
- **Exit:** none. See "Exit rules" below.
- **Starting equity:** $100,000. The real paper account (`PA3WTOH8U1VV`) holds
  $99,428.85 and predates the run; $100,000 is used as the clean baseline.
- **Sizing, structure choice, strike selection, risk limits:** all from the
  shipped code and `.env`, not chosen here.
- **Fees:** **not modelled.** No commission, no per-contract fee, no regulatory
  fee. Real per-contract costs would reduce every figure below.
- **Benchmark:** SPY buy-and-hold, entered at the same 25 August open and marked
  at the same 28 August close.

## Assumptions the data forced

### 1. The bid/ask spread is modelled, not observed

Alpaca publishes historical option **bars** and **trades**. There is no
historical option **quote** endpoint — only `latest`. So last week's bid/ask
cannot be retrieved. The harness sets `mid` = that contract's daily bar close and
places bid/ask symmetrically at `spread_pct`, snapped to the real tick grid
($0.01 below $3.00, $0.05 above).

`MAX_SPREAD_PCT = 0.10` is therefore gated on an **assumption**, so the run is
swept across the assumption rather than reported at one value:

| `spread_pct` | Trades filled | P&L |
|---|---|---|
| 0.5% | 5 | +$2,056.97 |
| 1% | 5 | +$2,024.89 |
| **2% (headline)** | **5** | **+$1,992.58** |
| 5% | 5 | +$1,754.71 |
| 10% | 5 | +$404.30 |

The result is stable from 0.5% to 5%. At 10% the `wide_spread` gate starts
firing (39 rejections) because the assumption equals the limit — that row shows
the gate working, not a different market.

### 2. Implied vol and greeks are solved, not fetched

Historical snapshots carry no IV or greeks, so the harness serves both as
`None`. That is **also what a paper account's live feed does**, so the project's
own Black-Scholes solve on the quote mid runs — the same code path as in
production, not a backtest-only shortcut.

### 3. Open interest is a single snapshot, and it is look-ahead

`open_interest` is passed through from Alpaca's contracts endpoint (11,183 of
15,684 contracts carry it). Every row is stamped `open_interest_date:
2026-08-27`. There is no point-in-time OI history, so a decision replayed on
24 August is gated on OI measured three days later. Open interest moves slowly,
so the distortion is small — but it is look-ahead and it is not removable with
this data source. A contract with no OI is served as `None`, which `RiskEngine`
correctly refuses (5 rejections).

### 4. A contract with no trade that day is not in that day's chain

11,888 of 15,684 contracts have a bar in the window. No trade printed means no
price to quote from. This doubles as a crude liquidity filter and biases the
selectable set toward liquid strikes.

### 5. Daily granularity

The live agent evaluates every 30 seconds. This replay evaluates once per day at
the close. Intraday entries, intraday gate flips, and same-day exits are all
outside what daily bars can represent.

### 6. The LLM leg is stubbed

`StrategistAgent`'s Featherless call is replaced by a fixed `RegimeRead` at
conviction 0.70, above the 0.55 `CONVICTION_FLOOR`. The matrix re-derives trend
and IV regime itself from the evidence pack and uses the LLM for exactly one
thing — the conviction veto — so this isolates the deterministic part of the
system and makes the run reproducible.

**This makes the trade count an upper bound.** A real LLM would veto some of
these on conviction. It also removes any risk of feeding the model a future date
as "today".

### 7. Exit rules do not exist

`PositionManagerAgent` marks positions to market and nothing else — no profit
target, no stop, no DTE exit, no thesis invalidation (`docs/PIPELINE-STATUS.md`
gap B). Rather than invent exit logic and measure code that does not exist,
positions are held and marked at Friday's close. The closing half-spread is
therefore **not** deducted; a real exit would cost more.

This is not a modelling nicety — it is the single biggest driver of the result.
See the report.

## Reproducing

```bash
export ALPACA_API_KEY=... ALPACA_SECRET_KEY=...
python backtests/runs/2026-08-30_universe_agent-replay_1Day/run.py
python backtests/runs/2026-08-30_universe_agent-replay_1Day/analyse.py
```

Raw CLI outputs and their SHA-256 digests are in `raw/` and
`data_fingerprint.json`. No network call happens after the fetch step.

## Caveats

- **Four trading days, five trades.** Nothing here is statistically meaningful.
  Any Sharpe ratio computed from four daily observations is arithmetic, not
  evidence, and is labelled as such in the report.
- One position (MSFT) supplies more than the entire net P&L. Remove it and the
  week is negative.
- No fees, no closing costs, no slippage beyond the modelled spread crossing.
- Open interest is mildly look-ahead (see above).

---

> **Important disclosure**
> This backtest is a hypothetical historical simulation and does not represent
> actual trading performance. Backtested results do not guarantee future results.
> Results depend on market-data quality, data feed selection, corporate-action
> handling, fees, slippage, liquidity, taxes, execution assumptions, and
> implementation details. This material is for research and educational purposes
> only and is not investment advice, a recommendation, an offer, or a
> solicitation to buy or sell securities, options, cryptocurrencies, or any other
> financial product. All investments involve risk and may lose value. Review
> Alpaca's disclosures and agreements at
> [alpaca.markets/disclosures](https://alpaca.markets/disclosures).

> Paper trading is a simulated environment. It does not involve real money or
> actual securities transactions. Paper results may differ from live trading
> because of fill assumptions, market impact, liquidity, latency, data
> differences, order handling, fees, and other market conditions.

No Alpaca trading-activity fees are modelled in this run, so no figure here
should be read as net of cost. Fee schedule:
<https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf>
