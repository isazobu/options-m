# options-m — exit rules v2 against the v1 week, 24–28 August 2026

Baseline: `2026-08-30_universe_exit-flow_1Day` · Same window, same cached data,
same fingerprint · Fill model: `next_open`, entries **and** exits · Modelled
spread: 2.00% · Fees: none

## Headline

| Run | Positions | Closed by rule | Total P&L |
|---|---:|---:|---:|
| v1 baseline | 6 | 1 | **+$2,702.62** (+2.70%) |
| v2 as shipped | 6 | 1 | **+$276.54** (+0.28%) |
| v2 with the debit target back at 50% | 6 | 1 | **+$2,702.62** (+2.70%) |

The third row is the whole finding. Put one parameter back and v2 reproduces
the baseline **exactly** — same six positions, same fills, same dates, same
cents. So the new machinery is neutral and the entire −$2,426 is attributable
to a single number: the debit profit target moving from 50% to 75%.

## What the parameter actually did

| | v1 | v2 |
|---|---|---|
| Tue 25 Aug close | MSFT debit call spread marked **+61.7%** → `profit_target` | +61.7% is below the 75% target → held |
| Wed 26 Aug | Exit fills, **+$1,160.34 realised**, slot freed | Marked **+82.1%** → `profit_target` |
| Wed 26 Aug close | Freed slot lets the matrix re-enter MSFT | Slot still occupied, no re-entry |
| Thu 27 Aug | Re-entry fills at 2.61, qty 9 | Exit fills, **+$754.26 realised** |
| Fri 28 Aug close | Re-entry marked **+$1,976.04** unrealised | Slot taken by IWM instead (−$43.96) |

Holding for the higher target earned more *on the position* — +82.1% against
+61.7% — and still finished $2,426 behind, because a day of delay cost the
re-entry. The baseline's advantage was never the exit threshold. It was one
freed slot landing on one trending name on one day, and $1,976 of that
advantage is an unrealised Friday mark, not money.

**Read this as a warning about the baseline, not a verdict on v2.** A result
that inverts when an exit fires one session earlier is a result dominated by
path, at n = 1. Neither number measures an edge.

## The DTE rungs

Fired once in the week: NVDA's iron condor signalled `dte_stop` at Friday's
close, 21 days from expiry, at +25.6%. There is no Monday in the window, so it
never filled and changed no P&L here. The expiry hard floor never triggered —
nothing in the window came within 2 days of expiry. The credit stop never
triggered either; no credit structure lost anything close to its credit.

So of the three rules added, exactly one was observable in this window, and
only the family thresholds moved money.

## What this does not tell us

Whether 75% is the right debit target. One week, one debit spread, one exit.
The honest conclusion is narrower: **the v2 ladder is correctly wired** — the
neutrality check proves the new payload fields perturb nothing — **and the
thresholds themselves remain unvalidated.** Sweeping them needs many more
windows than this.

## Reproducing

```bash
python backtests/runs/2026-08-31_universe_exit-rules-v2_1Day/run.py
EXIT_DEBIT_PROFIT_TARGET_PCT=0.50 python backtests/runs/2026-08-31_universe_exit-rules-v2_1Day/run.py
```

No network: the cached data comes from the baseline run's `raw/`.
