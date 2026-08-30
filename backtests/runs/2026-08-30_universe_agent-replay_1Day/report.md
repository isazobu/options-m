# options-m agent replay — 24–28 August 2026

Code under test: `main` @ `162aeae` · Fill model: `next_open` · Modelled spread: 2.00% · Fees: none

## Performance vs benchmark

| | Total Return | Max Drawdown | Sharpe | Final Equity |
|---|---:|---:|---:|---:|
| **Strategy** | **+1.99%** | −0.12% | *n/a* | **$101,992.58** |
| SPY buy-and-hold | +0.42% | −0.23% | *n/a* | $100,416.36 |

Annualised return is deliberately omitted and Sharpe is reported as *n/a*.
The run has **four daily observations and five trades**. Annualising a
four-day return produces a number in the thousands of percent, and a Sharpe
ratio from three daily returns is arithmetic, not evidence. The raw values are
in `summary.json` for completeness; they should not be quoted.

## The Teaching Five

1. **Total return vs benchmark** — +1.99% vs SPY +0.42%.
2. **Max drawdown** — −0.12% (SPY −0.23%). Low because only five positions
   were ever open and they were opened once.
3. **Number of trades** — **5 filled, out of 50 decisions evaluated.**
4. **Win rate** — 60% (3 of 5).
5. **Sharpe vs benchmark** — not computable at this sample size.

## The finding that matters more than the P&L

The agent opened its five positions **on the first day and never traded again.**

| Day | Filled | Blocked by position limits | Other rejections |
|---|---:|---:|---:|
| Mon 24 Aug | **5** | 3 | 2 |
| Tue 25 Aug | 0 | 9 | 1 |
| Wed 26 Aug | 0 | 7 | 3 |
| Thu 27 Aug | 0 | 8 | 2 |
| Fri 28 Aug | 0 | 8 | 2 |

`MAX_CONCURRENT_POSITIONS = 5` and `MAX_POSITIONS_PER_UNDERLYING = 1` filled on
Monday. From Tuesday on, **32 of 40 decisions were refused purely because the
book was full** — and it stayed full because `PositionManagerAgent` has no exit
rules (`docs/PIPELINE-STATUS.md` gap B). Nothing closes, so nothing frees a slot,
so the agent is a one-shot allocator with a four-day observation period bolted on.

The +1.99% is therefore **the return of five positions chosen on one day**, not
the return of a strategy running for a week. Exit rules would change this result
more than any parameter in `.env`.

## Trades

| Symbol | Structure | Signal | Fill | Qty | Entry | Mark | P&L | Max loss |
|---|---|---|---|---:|---:|---:|---:|---:|
| MSFT | debit call spread 515/530 | 24 Aug | 25 Aug | 6 | +1.97 | +6.01 | **+$2,426.34** | $1,788 |
| NVDA | iron condor 185/195 · 227.5/237.5 | 24 Aug | 25 Aug | 3 | −3.11 | −2.31 | +$238.53 | $1,996 |
| AAPL | long strangle 297.5P/325C | 24 Aug | 25 Aug | 3 | +5.87 | +6.35 | +$144.57 | $1,801 |
| QQQ | long strangle 679P/738C | 24 Aug | 25 Aug | 1 | +13.74 | +10.29 | −$344.60 | $1,324 |
| SPY | long strangle 746P/781C | 24 Aug | 25 Aug | 2 | +8.21 | +5.85 | −$472.26 | $1,610 |

Entry/mark are per-contract signed position values: positive is a debit paid,
negative is a credit received.

**First trade:** MSFT debit call spread, signalled 24 Aug, filled 25 Aug open.
**Last trade:** same day — there were no others.

Gross profit $2,809.44 · gross loss $816.86 · profit factor 3.44 · fees $0.

**One position is the result.** MSFT alone contributes +$2,426 against a net of
+$1,993; without it the week is −$434. MSFT rallied from 487 to 513 (+5.4%) and
the 515/530 spread went from 1.97 to 6.01. That is one directional call landing,
not a strategy edge, and five trades cannot distinguish the two.

## What the matrix asked for

Across all 50 decisions, before any gate:

| Structure | Times selected | Ever filled |
|---|---:|---|
| `long_strangle` | 31 | yes (3) |
| `iron_condor` | 8 | yes (1) |
| `call_debit_spread` | 6 | yes (1) |
| `put_debit_spread` | 3 | no |
| `put_credit_spread` | 2 | no |

The four structures added in PR #13 were reached **10 times** and one of them —
the NVDA iron condor — was actually traded. On the pre-PR-#14 build these ten
decisions could not have happened at all: ATM IV was read at the nearest expiry
(10–11 DTE) against 20-day realised vol, IV/RV sat at 0.45–0.62 everywhere, and
the matrix's entire expensive column was unreachable. After the fix, IV/RV
measured at 28–32 DTE ranged 0.70–1.26 and crossed the 1.10 threshold on IWM
(4 of 5 days), NVDA (3), SPY (2) and AAPL (1).

**PR #13 and PR #14 only work together.** #13 built the structures; #14 made the
signal capable of asking for them.

## Why the other 45 decisions did not trade

| Reason | Count |
|---|---:|
| `max_concurrent_positions` | 35 |
| `max_positions_per_underlying` | 18 |
| `low_open_interest` | 5 |
| `no_strike_within_increment` | 2 |
| `zero_quantity` | 2 |

(Reasons are collected, not short-circuited, so one rejection can carry several.)
`matrix.decide` returned `hold` **zero** times — with conviction stubbed above
the floor and no earnings blackout in the window, every symbol produced an intent
every day. The gates, not the signal, did all the filtering.

## Sensitivity to the modelled spread

Alpaca has no historical option quote endpoint, so bid/ask is modelled. Sweeping
the assumption:

| `spread_pct` | Trades | P&L |
|---|---:|---:|
| 0.5% | 5 | +$2,056.97 |
| 1% | 5 | +$2,024.89 |
| **2%** | **5** | **+$1,992.58** |
| 5% | 5 | +$1,754.71 |
| 10% | 5 | +$404.30 |

Stable from 0.5% to 5%. The 10% row is the `wide_spread` gate firing 39 times
because the assumption equals `MAX_SPREAD_PCT` — the gate working, not a
different market.

## Equity curve

| Date | Strategy | SPY |
|---|---:|---:|
| 24 Aug (pre-entry) | $100,000.00 | $100,000.00 |
| 25 Aug | $100,463.58 | $99,967.37 |
| 26 Aug | $100,629.58 | $99,989.56 |
| 27 Aug | $100,506.58 | $100,644.77 |
| 28 Aug | $101,992.58 | $100,416.36 |

Most of the gain lands on the final day, in one position.

## Data fingerprint

CLI: `alpaca` 0.0.14. Stock bars `feed=sip`, `adjustment=split`, 1Day,
2025-08-01 → 2026-08-28, 271 bars × 10 symbols. Option contracts: 15,684 in the
2026-08-31 → 2026-10-12 expiry window, ±15% strike band; 11,183 carry open
interest, all stamped `2026-08-27`. Option bars: 1Day, 2026-08-21 → 2026-08-28,
11,888 of 15,684 contracts have at least one bar. SHA-256 digests for every raw
file are in `data_fingerprint.json`.

## Caveats

- **Five trades over four days proves nothing.** Treat every number here as a
  pipeline integration test that happens to produce P&L.
- **No fees.** Per-contract and regulatory fees are not modelled; 30 contracts
  across 5 positions would incur real cost.
- **No closing cost.** Positions are marked at mid, not exited. A real exit pays
  another half-spread per leg.
- **Open interest is mildly look-ahead** — a single 27 Aug snapshot applied to
  decisions from the 24th onward.
- **The bid/ask spread is an assumption**, and it gates `MAX_SPREAD_PCT`.
- **The LLM is stubbed**, so 5 filled trades is an upper bound; a real conviction
  read would veto some.
- **Daily granularity** against an agent that runs every 30 seconds.

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

Fee schedule: <https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf>
