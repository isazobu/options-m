# options-m — position limit sweep and exit-flow evaluation, 24–28 August 2026

Code under test: `main` @ `6b32932` (includes PR #16, `feature/position-close`)
· Fill model: `next_open`, entries **and** exits · Modelled spread: 2.00% · Fees: none

This run extends `2026-08-30_universe_agent-replay_1Day` with the two things
that run reported as missing. Same window, same cached data, same fingerprint.
With `--no-exits` and the shipped limit it reproduces that run **exactly**
(+$1,992.58, the same five positions), so every difference below is caused by
the change under test and not by the harness.

## 1. Raising `MAX_CONCURRENT_POSITIONS` does almost nothing

| Limit | Exits off | Exits on |
|---|---:|---:|
| **5 (shipped)** | 5 pos · +$1,992.58 | 6 pos · **+$2,702.62** |
| 6 | 6 pos · +$1,646.99 | 7 pos · +$2,357.03 |
| 8 | 6 pos · +$1,646.99 | 7 pos · +$2,357.03 |
| 10 | 6 pos · +$1,646.99 | 7 pos · +$2,357.03 |
| 20 | 6 pos · +$1,646.99 | 7 pos · +$2,357.03 |

**The limit stops binding at 6, and P&L goes *down*.** Two separate reasons:

- **`MAX_TOTAL_PREMIUM_PCT = 0.15` takes over as the binding gate.** At limit 5
  the rejection ledger is `max_concurrent_positions=35`; at limit 8 it is
  `total_premium_exceeded=30` and `max_concurrent_positions` disappears
  entirely. Raising a limit that is no longer the constraint changes nothing.
  Anything above 6 is inert — the sweep rows are byte-identical.
- **The 6th position is worse than the 5 it joins.** It is not a new
  opportunity; it is the next-best candidate the matrix already ranked below
  them, and it dilutes. −$346 across the four days.

`MAX_POSITIONS_PER_UNDERLYING = 1` with a 10-symbol universe puts a hard
ceiling of 10 on concurrency regardless, so no value above 10 can ever mean
anything. **If the intent is to hold more risk, the parameter to raise is
`MAX_TOTAL_PREMIUM_PCT`, not `MAX_CONCURRENT_POSITIONS`.**

## 2. The exit flow works, and it is worth more than the limit

At the shipped limit of 5, turning exits on takes the week from **+1.99% to
+2.70%** — a bigger move than anything the limit sweep produced, and in the
opposite direction.

The mechanism, end to end:

| Day | Event |
|---|---|
| Tue 25 Aug | MSFT debit call spread fills at 1.97 |
| Tue 25 Aug close | Marked at +61.7% → `_close_reason` returns `profit_target` |
| Wed 26 Aug open | Exit fills, **+$1,160.34 realised**, slot freed |
| Wed 26 Aug close | Matrix picks MSFT `call_debit_spread` again — **the freed slot is what lets it in** |
| Thu 27 Aug open | Re-entry fills at 2.61, qty 9 |
| Fri 28 Aug close | Marked +84.0% → second `profit_target` signal, no session left to fill it |

So the exit rule did the thing the previous run said it could not: **it recycled
capital.** One slot turned over once in four days and that single turnover is
the entire +0.71% difference.

Note what it also cost. Held to Friday, the first MSFT spread was worth
+$2,426; exited at Wednesday's open it realised +$1,160. **The profit target
gave up $1,266 of that trade** and bought back more than that by redeploying
into a bigger position (qty 9 vs 6). That is the exit rule working as designed,
not free money — and with four days and one turnover, it is one coin flip.

## 3. The defect: `pnl_pct` is measured against the wrong denominator

`_compute_pnl_pct` divides unrealised P&L by `|market_value − unrealized_pl|`,
i.e. the **net entry value**. For a debit structure that is roughly the money at
risk and the percentage is meaningful. For a **credit** structure it is the
credit received, which is the smallest number in the trade.

Sweeping the thresholds exposes it:

| Profit target | Stop | Closed | P&L | Which positions exited |
|---|---|---:|---:|---|
| 25% | 25% | 3 | +$2,080.09 | MSFT ×2, **NVDA stop_loss** |
| 25% | 50% | 2 | **+$2,823.76** | MSFT ×2 |
| **50%** | **50%** | **1** | **+$2,702.62** | MSFT |
| 50% | 25% | 2 | +$1,958.95 | MSFT, **NVDA stop_loss** |
| 75% | 25% | 2 | −$467.13 | MSFT, **NVDA stop_loss** |
| 75% | 50% | 1 | +$276.54 | MSFT |
| 100% | 25% | 2 | +$867.61 | MSFT, **NVDA stop_loss** |
| 100% | 50% | 1 | +$1,611.28 | MSFT |

Every row with a 25% stop stops out the NVDA iron condor, and every one of
those rows is worse than its 50%-stop twin. The condor was opened for a net
credit of 3.11 against a max loss of 6.66 per contract. A "25% stop" therefore
fires at 0.78 of adverse move — **11.7% of the money actually at risk.** The
stop is not loose or tight; for credit structures it is measuring the wrong
thing, and it is roughly twice as sensitive as the same number means on a debit
spread.

PR #13 added four credit structures. The exit rule from PR #16 is denominated
in a way that treats them as far riskier than they are. **`pnl_pct` should
divide by `max_loss`**, which `OrderPlan` already carries and which is
comparable across debit and credit structures, or the two structure families
need separate thresholds.

Also note the profit-target column is not monotone (75% is the worst row, not a
middle one). With one recycled position driving everything, that column is
noise, not a tuning curve. Do not read an optimum out of it.

## 4. What the exit flow still does not have

- **The time stop never fires.** `exit_time_stop_days = 30` against a four-day
  window. Untested here, and the clock patch that would let it fire
  (`backtests/clock.py` now freezes `agents.strategist` too) is in place for a
  longer run.
- **No thesis invalidation.** `RegimeRead.invalidation` is written at entry and
  never read again. The close path is three arithmetic thresholds; the
  qualitative exit the strategist prompt promises does not exist.
- **No DTE-based exit.** A position walking into expiry week is closed by
  nothing.
- **Exits are not LLM-gated and that is correct** — but it also means the
  *entry* stub does not weaken the exit results here. The close numbers in this
  report are the shipped decision, not a stand-in.

## Caveats

Everything from the parent run still applies — four trading days, six positions,
one recycled slot, no fees, no closing commission beyond the modelled
half-spread, open interest mildly look-ahead, daily granularity, entry LLM
stubbed. **One position (MSFT, twice) is still the entire result.** Treat this
as an integration test of the exit path that happens to produce P&L, and re-run
it over a quarter before believing any threshold.

The exit fill *does* now pay the closing half-spread the parent run skipped, so
the +2.70% is net of one more real cost than the +1.99% was.

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
