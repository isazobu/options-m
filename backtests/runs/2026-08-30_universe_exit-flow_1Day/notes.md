# Exit-flow run — method notes

## The request

> "Raise the 5 limit. Also there is now a sell / position-exit flow — evaluate
> that too."

## What changed vs the parent run

`2026-08-30_universe_agent-replay_1Day` had no exits, because at commit
`162aeae` the system had none. PR #16 (`feature/position-close`) added one, so
this run models it. Same window, same `raw/` (symlinked, so the fingerprint in
`data_fingerprint.json` is the parent's and still valid), same everything else.

## The exit rule under test is the shipped one

`run.py` imports rather than restates it:

- `options_m.agents.position_manager._compute_pnl_pct` — builds `pnl_pct` from
  `market_value` / `unrealized_pl` exactly as the cache writer does.
- `options_m.agents.strategist._close_reason` — the profit target / stop loss /
  time stop decision, including their precedence.

The harness supplies only the payload those two read: `market_value`,
`unrealized_pl`, `opened_at`. So the thresholds, their order, and their
denominator are production's, and the `pnl_pct` finding in the report is a
finding about shipped code, not about this file.

**The close path never calls the LLM.** `_close_reason` is pure arithmetic over
the position cache. The entry-side conviction stub therefore does not weaken the
exit results — unlike the entry counts, they are not an upper bound.

## Timing

```
entry signal -> day D close      entry fill -> day D+1 OPEN
close signal -> day D close      exit  fill -> day D+1 OPEN
final mark   -> 28 Aug close, mid, for whatever is still open
```

Within a day the order is: exit fills at the open → entry decisions at the close
→ close decisions at the close. That last ordering matches `StrategistAgent`,
which runs its close sweep before its open sweep.

A position with a close signalled on day D **keeps its slot** until the exit
fills on D+1. The broker still reports it as open, so
`concurrent_option_positions` still counts it. This is deliberately the
conservative choice: it means a freed slot is available one day later than an
optimistic model would give it, and the +0.71% in the report survives that.

## Exit fill pricing

Mirror image of the entry. A leg we are long is sold at the **bid**, a leg we
are short is bought back at the **ask**, both at the next session's open,
`spread_pct` wide. The parent run marked at mid and never paid this — so the
exit-on numbers carry one real cost the exit-off numbers do not.

## Clock

`backtests/clock.py` gained `options_m.agents.strategist` in `_PATCH_TARGETS`,
because `_close_reason` dates the time stop from `datetime.now(UTC)`. It cannot
fire in a four-day window against a 30-day stop, so this changes no number here
— it is in place so a longer run is correct rather than accidentally correct.

## Parity check

```bash
python run.py --no-exits          # +$1,992.58, 5 positions — the parent run exactly
python run.py                     # +$2,702.62, 6 positions, 1 closed
python run.py --max-concurrent 8  # sweep the limit
```

The `--no-exits` run reproducing the parent to the cent is what licenses
attributing every difference to the change under test.

## What this run does NOT establish

- Four trading days and **one** recycled slot. The +0.71% from exits is a single
  turnover of a single position. It is a mechanism check, not an edge estimate.
- The time stop is untested (never fires in this window).
- The threshold sweep is not a tuning curve — it is not monotone in the profit
  target, because one position drives it. The *stop-loss* column is a real
  finding (every 25%-stop row is worse, and always for the same structural
  reason); the profit-target column is noise.
- No fees. Two more legs traded means more unmodelled per-contract cost than the
  parent run.
