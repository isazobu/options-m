# Campaign Exit Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the two-session campaign deployable by hardening exits, adding deterministic campaign flattening, removing hidden pacing/IV gates, and applying the complete Render risk envelope.

**Architecture:** Exit classification and proposal creation move into a broker-free `exits` module called by `PositionManagerAgent` every 60 seconds. `ExecutionAgent` always processes closes during a kill switch, uses per-leg close intents, starts urgent exits at an aggressive limit, and reprices working close limits on a timed ladder. Runtime campaign parameters remain environment-driven through `render.yaml`.

**Tech Stack:** Python 3.12, asyncio, Pydantic Settings, pytest, Ruff, MyPy, Render Blueprint YAML, Alpaca MCP.

## Global Constraints

- Keep Alpaca paper-only and `DRY_RUN=false`.
- Do not set `REPLAY_LAST_SESSION`.
- New opens stop after the two-session campaign but remain available on session two.
- Kill switch freezes opens and never blocks exits.
- Entry DTE is 1–2 days; both DTE exit floors are 0.
- All broker writes continue through `AlpacaMcp`.

---

### Task 1: Extract and accelerate close proposals

**Files:**
- Create: `src/options_m/exits.py`
- Modify: `src/options_m/agents/position_manager.py`
- Modify: `src/options_m/agents/strategist.py`
- Modify: `src/options_m/agents/__init__.py`
- Test: `tests/test_position_manager.py`
- Test: `tests/test_strategist.py`

**Interfaces:**
- Produces: `evaluate_close_proposals(store, settings, notifier, now=None) -> dict[str, Any]`
- Produces: `close_reason(payload, settings, now=None) -> str | None`
- Consumes: cached positions, pending proposals, campaign calendar.

- [ ] Write tests proving PositionManager writes threshold and final-session flatten proposals.
- [ ] Run targeted tests and verify they fail before implementation.
- [ ] Extract deterministic close logic and call it after PositionManager refreshes the cache.
- [ ] Remove close evaluation from Strategist's 300-second run path.
- [ ] Run targeted tests and verify they pass.

### Task 2: Make close execution fail-safe

**Files:**
- Modify: `src/options_m/agents/execution.py`
- Modify: `src/options_m/mcp_client.py`
- Modify: `src/options_m/config.py`
- Test: `tests/test_execution.py`

**Interfaces:**
- Produces: close orders with `buy_to_close` or `sell_to_close` on every leg.
- Produces: `AlpacaMcp.replace_order_by_id(order_id, limit_price)`.
- Consumes: `limit_price_spread_nudge_pct`, `close_reprice_seconds`, and `close_reprice_max_attempts`.

- [ ] Write tests proving kill switch permits closes and blocks opens.
- [ ] Write tests proving urgent exits cross the mark and every close leg has the correct intent.
- [ ] Write tests proving stale working close orders move up the reprice ladder.
- [ ] Run tests and verify the expected failures.
- [ ] Move kill-switch gating below the close branch.
- [ ] Add urgent initial pricing and timed close-order replacement.
- [ ] Run targeted tests and verify they pass.

### Task 3: Make missing IV explicit

**Files:**
- Modify: `src/options_m/matrix.py`
- Test: `tests/test_matrix.py`

**Interfaces:**
- Produces: `_iv_regime(...) -> cheap | expensive | very_expensive | unknown`.
- Consumes: ATM IV and 20-session realized volatility.

- [ ] Write a test proving missing IV/RV produces `hold`.
- [ ] Run it and verify it fails because missing data currently aliases to `cheap`.
- [ ] Return `unknown` and stop before the matrix lookup.
- [ ] Run matrix tests and verify they pass.

### Task 4: Apply the Render campaign envelope

**Files:**
- Modify: `render.yaml`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: explicit Render env values for sizing, risk, campaign, proposal pacing, exits, liquidity, and DTE.

- [ ] Add a blueprint test asserting every requested key/value and absence of replay mode.
- [ ] Run it and verify it fails.
- [ ] Add all campaign variables to `render.yaml`.
- [ ] Run config tests and verify they pass.

### Task 5: Verify and publish

**Files:**
- Verify all modified Python and YAML files.

- [ ] Run targeted pytest suites.
- [ ] Run the full pytest suite.
- [ ] Run Ruff and MyPy.
- [ ] Inspect the final diff for secrets and unintended changes.
- [ ] Create a feature branch, commit, push, and open a GitHub pull request.
