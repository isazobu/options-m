# Notes — exit-rules-v2 replay, 24–28 August 2026

## What this run is for

One question: what do the v2 exit rules do to a week the v1 rules already
traded? Everything else is held fixed — same window, same cached bars and
contracts (`../2026-08-30_universe_exit-flow_1Day/raw`, same fingerprint), same
fill model, same 2.00% modelled spread, same fixed conviction of 0.70.

## What changed under the harness

`_close_reason` went from one threshold pair to a five-rung ladder, and
`PositionManagerAgent` now writes the fields it reads. `position_payload` here
was extended to match: `net_value`, `min_dte`, `has_short_leg`.

`mid_value` was already signed per contract, so the net this harness hands over
is the same number it always computed — only the gross is new, and nothing
reads it. That is the reason the neutrality check below comes out exact.

## Assumptions and limits

- **n = 1.** Five sessions, six positions, one exit. Nothing here generalises;
  it isolates a mechanism, it does not measure an edge.
- **No option quotes.** Alpaca serves no historical option quotes, so every
  fill crosses a modelled 2.00% spread rather than a real one. The exit fills
  inherit that fiction exactly as the entries do.
- **Unrealised marks are not P&L.** Positions still open on Friday are marked
  at the mid. The single largest number in the v1 baseline is one of these.
- **The last day cannot fill.** A close signalled at Friday's close has no
  Monday in the window, so it shows as a signal and never as a fill.
