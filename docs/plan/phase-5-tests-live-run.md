# Phase 5 — Hardening and the live paper run

**Master doc:** `00-MASTER.md`
**Prerequisite:** Phase 4 complete — the full loop runs: candidates → regime read → matrix
verdict → risk gate → order → position management → exit → lesson, all visible in the
dashboard, and `options-m flatten` works against a seeded set of positions.
**Goal:** make it survive being left alone, and then leave it alone. By the end of this phase
the deployed service must have accumulated real trade history on the dedicated paper account,
because that is what the judges will actually look at.

Start this phase early in the day. **The live run needs at least one full trading session,
ideally two**, and per `00-MASTER.md`'s operational-window section there are only about 3
full sessions total before the 4 Sep 15:00 UTC deadline — do not let this phase slip.

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

## The account requirement (do this first, it gates everything)

Hackathon rule #4: a **brand-new Alpaca paper trading account dedicated to this hackathon,
with a $100,000 starting balance.**

1. Create the fresh paper account and reset starting equity to exactly $100,000.00.
2. Generate new API keys; put them in Render's dashboard as secrets, never in the repo.
3. Record the **Account ID** — the submission form asks for it so judges can inspect live P/L.
4. Confirm the options level on that account. If it is Level 2, set `OPTIONS_LEVEL=2` so
   `strategy_builder` and `risk.py` disable verticals and fall back to single long legs.
5. Verify nothing from an older account leaks in: no stale `.env`, no cached keys.
6. Wipe the Neon database and re-run migrations so the equity curve starts clean at $100k.
   A chart that starts at the exact starting balance is worth more than any slide.

`VibeHedge` leaked a real paper Account ID into `.env.example` and its write-up. Check ours
does not: `git grep -iE "account.?id|APCA|ALPACA_(API|SECRET)"` should return only names.

---

## Hardening checklist

### Resilience

- [ ] Kill the MCP subprocess while the service runs → the client reconnects and the agents
      recover through the supervisor's backoff. No crash loop.
- [ ] Point `DATABASE_URL` at a dead host → `/health` stays 200, `/ready` returns 503, the
      process survives.
- [ ] Revoke the Featherless key → `StrategistAgent` marks proposals `llm_failed`;
      `PositionManagerAgent` keeps closing positions normally.
- [ ] Send SIGTERM → agents finish their current iteration, uvicorn drains, the pool closes,
      exit code 0 within `SHUTDOWN_GRACE_SECONDS`. (The skeleton already does this; verify it
      still holds with five agents.)
- [ ] Leave it running across a market close and the next open → no orders while closed, and
      it resumes cleanly.
- [ ] Confirm Render is not sleeping the service: check UptimeRobot's log and the Render
      instance-hours counter. 750 free hours/month is 744 in a 31-day month, so this must be
      the only always-on free service in the workspace.
- [ ] Watch Neon compute hours. 100/month, and every DB touch keeps compute awake. If the
      burn rate is too high, batch writes harder and lengthen the `MarketPulseAgent` interval.

### Correctness sweep

- [ ] `git grep -nE "550\.0|100000\.0|SIMULATED|FILLED_SIMULATED"` — no hardcoded prices or
      synthetic fills anywhere.
- [ ] No `except Exception: return <default>` on any path that produces a number a trade
      depends on. Search for silent fallbacks.
- [ ] Every write-tool call is enforced by `dry_run` at the transport layer, not the call site.
- [ ] Every submitted order carries a `client_order_id`.
- [ ] `risk.py` imports nothing from `matrix.py`, `llm.py`, or `trading/strategist.py`.
      Add a test that asserts this by inspecting the module's imports.
- [ ] Every money column is `numeric`, never `double precision`.
- [ ] No secret appears in any log record at any level.
- [ ] Grep confirms zero calls to `get_clock`, `get_account_info`, `get_all_positions`, or
      `get_open_position` outside their one owning agent (`market_pulse.py` /
      `position_manager.py`) — the local-cache design from `00-MASTER.md` is actually load-bearing,
      not just documented.
- [ ] `earnings.py`'s `LAST_REFRESHED` is still recent relative to the live-run date; if the
      run slipped past the original ~4.5-day window, re-verify every date before trusting the
      gate.

### Coverage

- [ ] `pytest --cov` — get `risk.py`, `strategy_builder.py`, and `mcp_client.py` above 90%.
      These three decide whether real orders are correct; the rest can be thinner.
- [ ] One end-to-end test with a fake MCP server and a stubbed LLM driving the whole chain
      from candidate to closed trade with a lesson, asserting the row written at each stage.

### Performance and cost

- [ ] Log and check per-iteration duration for every agent — nothing should approach its
      own interval.
- [ ] Total Featherless spend per trading day, projected from `llm_calls`. Tune
      `strategist_interval_seconds` and `llm_max_tokens_*` so the $25 voucher comfortably
      covers the run through the deadline, with headroom for the demo.
- [ ] Confirm the evidence pack stays under its size budget for the widest chain in the
      universe (SPY). Truncate the chain summary, never the risk data.

---

## The live run

1. Set `DRY_RUN=false`, `KILL_SWITCH=false` on Render. Redeploy and confirm the first
   proposal appears.
2. Let it trade a full session unattended.
3. Check in at the close: how many proposals, how many approved, how many rejected and by
   which rule, realised P/L, any agent with a rising failure count.
4. Tune based on what you see, not on what you expected:
   - Zero trades in a session almost always means the risk gates or liquidity filters are too
     tight, or the conviction floor is too high. Loosen the *thresholds*, never the safety rules.
   - Too many trades means the conviction floor is too low or the per-underlying cap is not
     binding.
   - Rejections concentrated on one rule tell you exactly what to adjust.
   - Remember the earnings gate is expected to be a no-op this run (see `00-MASTER.md`) — if
     it is rejecting trades, something is miscalibrated (a wrong date, or the window too
     wide), not working as intended.
5. Freeze all behaviour changes 24 hours before the deadline. After that, only capture
   material and fix outright breakage.
6. **Run the wind-down.** Roughly 2–3 hours before 4 Sep 15:00 UTC, confirm `risk.py`'s
   wind-down cutoff has stopped new entries, then run `options-m flatten` and verify it exits
   0 with every position closed and a `trades` row written for each. Do this with enough time
   left to react if a close order does not fill promptly — do not run it for the first time
   at 14:55 UTC.

**Capture as you go** — this is the raw material for Phase 6 and you cannot recreate it later:
- Screen recording of the dashboard with real positions open and a decision chain expanded.
- Screenshot of the Alpaca paper account showing the option positions.
- A copy of the most interesting decision chain (evidence → regime read → matrix verdict →
  contracts → order) as text.
- A screenshot of the risk-events feed showing declined trades.
- The equity curve at its most representative.
- A screenshot or log excerpt of `options-m flatten` closing out the book cleanly before the
  deadline.

---

## Acceptance criteria

- [ ] Fresh $100k paper account live, Account ID recorded.
- [ ] At least one full unattended trading session completed on the deployed service.
- [ ] Real option positions opened **and** at least one closed by `PositionManagerAgent`,
      with a lesson written.
- [ ] Every resilience check above passes.
- [ ] `ruff check . && mypy && pytest --cov` green, with the three critical modules >90%.
- [ ] All demo material captured.

---

## Traps

- Do not start the live run on the last day. One session is the minimum; two is safe.
- Do not tune by loosening a safety rule to get a trade — tune thresholds only.
- Do not redeploy during the demo window.
- Do not forget that Render sleeps without the pinger and the agents stop silently with no
  alert. Check the pinger itself.
