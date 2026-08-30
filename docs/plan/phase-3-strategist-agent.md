# Phase 3 — Featherless LLM layer and the single `StrategistAgent`

**Master doc:** `00-MASTER.md`
**Renamed from `phase-3-llm-crew.md`** (2026-08-29): there is no crew anymore. This phase
now builds one LLM call inside one agent, not three parallel analysts plus a judge.
**Prerequisite:** Phase 2 complete — evidence packs, `strategy_builder`, `risk.py` and
`ExecutionAgent` all working; a hand-written `StrategyIntent` already produces a real paper
options order; the local-cache tables (`market_calendar`, `account`, `positions`) exist and
are kept current.
**Goal:** replace the hand-written intent with a genuine LLM-driven read, gated by a
deterministic strategy matrix, running on Featherless. At the end of this phase the system
is genuinely autonomous.

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

## Design change from the original plan: one agent, not a crew

The original version of this phase specified three parallel analysts (Bull, Bear,
Volatility) plus a Portfolio-Manager judge, reading price action *and* news. That design is
retired. The replacement, decided directly by the user and now canonical:

- **`StrategistAgent` is architecturally one agent** — one process registration, one box in
  the diagram, one `step()`. It is not decomposed into separate sub-agent processes.
- Inside that single `step()`, the work happens in four sequential sub-steps (not four
  agents): **filter candidates → collect evidence → one LLM call for the regime read →
  deterministic Strategy Matrix + earnings gate.** Only the third sub-step touches an LLM.
- **The analyst crew is gone.** Drop `bull_analyst.md`, `bear_analyst.md`,
  `volatility_analyst.md` and `portfolio_manager.md` from the original plan.
  **Corrected:** this section previously read "News is gone entirely… and therefore
  no prompt-injection fence to build." That was wrong, and the error survived into
  the code. `evidence.py` ships an `untrusted_news` field, `.env` carries the `news`
  toolset, and for a while the strategist prompt fenced none of it while the chat
  path fenced the identical data. The fence now lives in
  `src/options_m/prompts/external_text_fence.md` and is shared by both paths.

### Why the LLM's job is narrower than it first looks

Trend classification (SMA20/50 + ADX) and volatility-regime classification (IV/RV ratio vs.
the 1.10 / 1.40 thresholds) are both already computed **deterministically in `evidence.py`**
(Phase 2) — they do not need a model at all. What is left for the LLM is exactly the part
that is not a threshold lookup: reading the evidence pack as a whole and producing a
**thesis** (why this setup, in plain language), an **invalidation** (what would prove the
read wrong), and a **conviction** score — the qualitative judgment a deterministic threshold
cannot produce, but that is valuable on the dashboard and as an input the earnings/matrix
gate does not otherwise have. The LLM does **not** re-derive trend or IV/RV itself (both are
handed to it as already-classified facts in the evidence pack, not raw numbers to interpret)
and it does **not** pick the strategy family — that is the matrix's job, in code, always.
This is "Structure gating in code, not prompt" applied even more strictly than the original
crew design: the model narrates and scores a reasoning already reached by threshold, it
never negotiates a threshold.

---

## Featherless facts

- OpenAI-compatible. Base URL `https://api.featherless.ai/v1`, endpoints
  `/chat/completions`, `/completions`, `/models`.
- Auth: `Authorization: Bearer $FEATHERLESS_API_KEY`.
- Hackathon sponsor voucher code `ALPACAA26` ($25 credit).
- Serves open-weight models. **One tier now, not two** — the fast-tier analyst crew is gone,
  so there is only `FEATHERLESS_MODEL_DEEP`, a single mid/large instruct model, called once
  per `StrategistAgent` iteration. Confirm the exact model id against `GET /v1/models` at
  implementation time — **do not hardcode a model id in source.**

We call it with plain `httpx` (already a prod dependency after Phase 1). No OpenAI SDK, no
LangChain. Featherless models are open-weight and their tool-calling support is uneven, so
we do **not** rely on function calling — we ask for JSON and validate it ourselves.

---

## Deliverables

### 1. `src/options_m/llm.py`

```python
class Llm:
    async def complete_json[T: BaseModel](self, *, schema: type[T], system: str,
                                          user: str, max_tokens: int,
                                          temperature: float) -> T
```

Requirements (mostly unchanged from the original plan — the crew is gone, the client is not):

- One shared `httpx.AsyncClient` with an explicit timeout, opened/closed in the app
  lifecycle alongside `Database` and `AlpacaMcp`.
- **One tier from config**: `featherless_model_deep`. There is no `_fast` tier — remove it
  from config if it was already added, since there are no analysts left to run on it.
- **Structured output by validation, not by trust.** `complete_json` appends the JSON schema
  to the prompt, sets `response_format={"type": "json_object"}` when the model accepts it,
  extracts the first balanced JSON object from the reply (models wrap output in prose and
  code fences), and validates with pydantic.
- **One repair retry**: on a validation failure, re-prompt once with the raw output and the
  validation error appended. If it fails again → raise `LlmContractError`.
- **Fail closed.** A failed decision means *no trade*, recorded as
  `proposals.status='llm_failed'` with the error. Never fall back to free text for a trade
  decision. (`TradingAgents/.../utils/structured.py:73-89` silently falls back to prose,
  which is fine for a report and unacceptable for an order.)
- Record every call in an `llm_calls` table: role, model, prompt+completion tokens, latency,
  ok/error. Enforce a configurable daily token budget; exceeding it engages a soft halt on
  `StrategistAgent` only (never on `PositionManagerAgent` — exits must always work).
- Never log the API key; never log full prompts at INFO (DEBUG only).

### 2. The regime-read output type and prompt

```python
class RegimeRead(BaseModel):
    """The one thing the LLM produces each StrategistAgent iteration."""
    thesis: str
    invalidation: str
    conviction: float          # 0..1
    # trend and iv_regime are NOT re-decided here — they are already classified in
    # evidence.py and simply echoed back for the model to see and reason from, so the
    # prompt below asks it to acknowledge rather than re-derive them.
```

Single prompt file: `src/options_m/prompts/strategist.md`, loaded by a small
`prompts/loader.py` with a path-escape guard — it makes the prompt configuration rather
than code, so it can be iterated on without a redeploy. **Two corrections:** rendering is
`string.Template` (`$var`), not `str.format_map`, because these prompts are full of JSON
and doubling every literal brace was a standing hazard; and the "without a redeploy" claim
holds for a local, editable or bind-mounted checkout only — the Docker image installs into
a root-owned venv and runs as `app`, so the shipped copies are read-only there.

The prompt must state (the first two sentences below are no longer repeated here — they
are `evidence.NOTE`, which the model already reads inside the pack, so the prompt points
at that field instead of restating it):

> Fields marked `NO_DATA_AVAILABLE` are genuinely unavailable. Do not estimate, infer, or
> fabricate values for them. The trend and volatility-regime classifications in this evidence
> pack are already computed deterministically — do not re-derive or contradict them; use them
> as given facts. You may not name option contracts, strikes, expiries, or a strategy — the
> matrix and the execution layer decide those. Output only the requested JSON: thesis,
> invalidation, conviction.

A news-fencing paragraph **is** needed: the pack this prompt sees carries
`untrusted_news`. It is stated immediately ahead of the pack, from the shared
`external_text_fence.md` fragment, so the fence and the content it governs cannot be
separated by an edit to either path alone.

### 3. `src/options_m/matrix.py` — the deterministic Strategy Matrix + earnings gate

Pure code, zero LLM, zero MCP writes — same "no imports from the reasoning layer" discipline
as `risk.py`.

```python
def decide(evidence: dict, regime: RegimeRead) -> StrategyIntent | Literal["hold"]:
    ...
```

1. **Earnings gate first, before the matrix lookup.** If `evidence["earnings_blackout"]` is
   true, return `"hold"` immediately — do not even run the matrix. (This is the primary
   place the gate lives; `risk.py` checks it again independently as a backstop in case a
   candidate somehow reaches `ExecutionAgent` without going through this path.)
2. **Matrix lookup** on `evidence["trend"]` (`yukarı`/`yatay`/`aşağı`) ×
   `evidence["iv_regime"]` (`pahalı`/`ucuz`, with the `çok pahalı` tier at IV/RV ≥ 1.40
   upgrading the flat/expensive cell):

   | | Prim pahalı (IV/RV ≥ 1.10) | Prim ucuz |
   | --- | --- | --- |
   | **Yukarı eğilimli** | `put_credit_spread` | `call_debit_spread` |
   | **Yatay** | `iron_condor` (→ `iron_butterfly` if IV/RV ≥ 1.40) | `long_strangle` |
   | **Aşağı eğilimli** | `call_credit_spread` | `put_debit_spread` |

3. **Level degradation.** If `evidence["options_trading_level"]` (from the local `account`
   cache) is below what the chosen structure needs, downgrade: any 2-or-4-leg structure with
   a short leg needs Level 3; if the account is Level 2, downgrade `call_debit_spread` /
   `put_debit_spread` to `long_call`/`long_put` at the same target delta, and downgrade every
   credit/condor/butterfly cell to `"hold"` (there is no safe single-leg equivalent for
   selling premium). `long_strangle` needs only Level 2 and is never downgraded.
4. **Assemble the `StrategyIntent`**: `target_delta`/`target_short_delta` and `spread_width`
   come from config defaults per structure (the calibration table in `00-MASTER.md` and
   Phase 2 §2.5), `dte_min`/`dte_max` from config, and `thesis`/`invalidation`/`conviction`
   copied straight from the LLM's `RegimeRead`. The matrix never invents its own thesis text
   — it is a router, not a second opinion.
5. Return `"hold"` if `regime.conviction` is below a configurable floor, even when the matrix
   would otherwise produce a structure — conviction is the one LLM-sourced number allowed to
   veto a trade, since the deterministic reads alone do not know when a setup is genuinely
   marginal.

### 4. `src/options_m/trading/strategist.py` — `StrategistAgent`

Cadence 5–15 min, configurable, market-hours-only. Still **one agent, one `step()`**:

```
step():
  1. if not store.market_is_open(now()): return early           # local cache, no MCP call
  2. if kill switch or LLM budget exhausted: return early
  3. candidate = store.top_candidates() filtered to skip:
       - symbols with an open position (local `positions` cache)
       - symbols with an in-flight proposal (per-underlying cap)
       - symbols inside earnings.is_earnings_blackout(symbol, today)   # cheap pre-filter,
                                                                        # before even looking
                                                                        # at the cached pack
     if none: return early
  4. pack = store.get_cached_evidence(candidate)        # LOCAL READ ONLY. Written by
                                                         # MarketPulseAgent every 60s -- this
                                                         # agent never calls evidence.collect()
                                                         # or any MCP tool for market data.
     if pack is None or pack is stale (older than ~2x MarketPulseAgent's interval):
       log and return early -- do not reason over data that was never actually collected
  5. regime = await llm.complete_json(schema=RegimeRead, ...)   # the ONE LLM call, and the
                                                                  # only I/O this step() does
                                                                  # besides the local reads
  6. intent = matrix.decide(pack, regime)               # deterministic, see matrix.py above
  7. if intent == "hold":
       store.create_proposal(status='no_action', evidence=pack, llm_read=regime,
                              matrix={"result": "hold", "reason": ...})
       return
  8. store.create_proposal(status='pending', intent=intent, evidence=pack,
                            llm_read=regime, matrix={"result": intent.strategy, ...})
```

**This step makes zero MCP calls.** Every input (market-open check, candidate ranking,
evidence pack) is a local Postgres read; the only outbound call in the whole iteration is the
one LLM request. That is the concrete benefit of moving `evidence.py`'s ownership to
`MarketPulseAgent` (see `00-MASTER.md`'s "who calls the evidence tool" section): the slow,
data-heavy work runs on a fast, cheap, no-LLM cadence, and this loop — which can legitimately
take 5-15 minutes between iterations — never blocks on a chain fetch.

`ExecutionAgent` from Phase 2 picks up `pending` proposals unchanged — the two agents are
decoupled through the `proposals` table, which is why the LLM can be slow or fail without
ever blocking execution or position management.

An `LlmContractError` from step 5 is caught inside `step()` and recorded as
`proposals.status='llm_failed'`; it does not propagate out and does not stop
`ExecutionAgent`/`PositionManagerAgent` from continuing normally.

### 5. Config additions

`featherless_api_key`, `featherless_base_url`, `featherless_model_deep`,
`llm_timeout_seconds`, `llm_max_tokens`, `llm_daily_token_budget`,
`strategist_interval_seconds`, `options_level` (1/2/3), `conviction_floor` (default e.g.
0.55), plus the per-structure delta/width defaults consumed by `matrix.py`
(`short_delta_default`, `spread_width_default`, `dte_min`, `dte_max`). Remove
`featherless_model_fast` and `enable_second_debate_round` if either was already added by an
earlier draft of this plan — there is no fast tier and no debate round anymore.

---

## Tests

- `test_llm.py` — JSON extraction from prose-wrapped and fence-wrapped replies; repair retry
  fires exactly once; `LlmContractError` after two failures; token budget halt; the API key
  never appears in log records.
- `test_matrix.py` — every one of the 6 matrix cells resolves to the documented structure;
  the IV/RV ≥ 1.40 upgrade from iron condor to iron butterfly; the earnings gate short-circuits
  before the matrix lookup runs (assert the matrix function is never reached, not just that
  the result is `hold`); Level-2 degradation downgrades debit spreads to long calls/puts and
  holds every credit/condor/butterfly cell; a conviction below the floor forces `hold` even
  when the matrix would otherwise produce a structure.
- `test_strategist.py` — closed market (via the local calendar cache) returns early with no
  LLM call and no MCP call at all (assert the fake `AlpacaMcp` records zero calls for the
  whole test — this agent should never need one); an existing position in a symbol skips it
  (local cache, not a live `get_open_position`); a symbol inside its earnings blackout is
  skipped **before** its cached evidence row is even read (assert
  `store.get_cached_evidence` is never called for it); a candidate with no cached evidence
  row yet (or a stale one) is skipped rather than reasoned over with `NO_DATA_AVAILABLE`
  everywhere; `hold` writes a `no_action` proposal and never a pending one; an
  `LlmContractError` marks the proposal `llm_failed` and does not raise out of `step()` more
  than the supervisor can absorb.
- `test_prompts.py` — the single `strategist.md` template renders with the expected keys; the
  loader rejects a path-escaping name.

---

## Acceptance criteria

- [ ] With real Featherless and Alpaca keys, `StrategistAgent` produces a proposal whose
      `llm_read` JSONB contains a thesis/invalidation/conviction, and whose `matrix` JSONB
      shows which cell of the strategy matrix fired.
- [ ] `ExecutionAgent` turns it into a real paper options order with no code change.
- [ ] A deliberately malformed model reply results in `llm_failed`, never in an order.
- [ ] A symbol inside its earnings blackout never reaches an LLM call.
- [ ] `StrategistAgent.step()` makes zero MCP calls in every test — grep and a runtime
      assertion both confirm it only reads the local `evidence`/`positions`/`market_calendar`
      caches and calls the LLM.
- [ ] `ruff check . && mypy && pytest` green.

---

## Traps

- Do not let the LLM name a contract or pick the strategy family. The schema plus
  `matrix.py` are the enforcement.
- Do not fall back to free text for a decision — fail closed.
- Do not let the LLM re-derive trend or IV/RV from raw numbers — it only sees the
  already-classified facts, so there is nothing for it to get wrong there.
- Do not call `evidence.collect()` from this agent at all — that call belongs to
  `MarketPulseAgent` now. `StrategistAgent` only ever reads `store.get_cached_evidence()`.
- Do not read the cached evidence row before the earnings-blackout pre-filter — that ordering
  is what keeps a blacked-out symbol cheap to skip (a local read is cheap, but skipping it
  entirely is still cheaper and keeps the contract simple: no reasoning happens on a symbol
  that cannot trade regardless of what the evidence says).
- Do not hardcode a Featherless model id in source; env only.
- Do not resurrect the fast/deep two-tier split or a debate loop — there is one model call
  per iteration by design now.