# Phase 6 — Submission package

**Master doc:** `00-MASTER.md`
**Prerequisite:** Phase 5 complete — the service is live on the dedicated $100k paper
account with real trade history, and all demo material is captured.
**Deadline: 4 September 2026, 15:00 UTC.** Work backwards from that. Have everything
submitted with hours to spare; the form itself takes longer than you expect.

Everything in this phase goes into `options-m/docs/submission/`.

---

## Required deliverables

Verified against the hackathon rules and the requirement matrix in
`../VibeHedge/implementation_plan.md`.

| # | Deliverable | File |
| --- | --- | --- |
| 1 | 1-page write-up covering the AI logic, the risk gates, and how Alpaca's infrastructure is used | `WRITEUP.md` (+ PDF export) |
| 2 | Demo video (~3 min) | `VIDEO_SCRIPT.md` → recorded MP4 / YouTube link |
| 3 | Slide deck (PDF) | `SLIDES.md` → exported PDF |
| 4 | Submission metadata for the LabLab form | `SUBMISSION_METADATA.md` |
| 5 | Cover image, 16:9 | `cover.png` |
| 6 | Build-in-public posts (up to 5, tagging `@lablabai` and `@AlpacaHQ`) | `social_posts.md` |
| 7 | Public repository | GitHub, with a README a judge can follow |
| 8 | Paper Account ID | in the metadata doc, for judge inspection |

---

## `SUBMISSION_METADATA.md` — exact constraints

- **Title** — short, concrete, names the thing.
- **Short description** — **50–255 characters**. Count it.
- **Long description** — **minimum 100 words, 600–2000 characters**. Count it.
- **Categories** — e.g. Autonomous Agents, Algorithmic Trading, Options, Risk Management.
- **Technologies used** — Alpaca Trading API, Alpaca MCP Server v2, Alpaca Agent Skills,
  Model Context Protocol, Featherless AI, Python 3.12, FastAPI, asyncio, PostgreSQL (Neon),
  Docker, Render.
- **Paper Account ID.**

Write the character counts into the file next to each field, the way
`VibeHedge/submission/SUBMISSION_METADATA.md` does. It makes the final copy-paste trivial.

---

## `WRITEUP.md` — the one page

Lead with what makes this different, not with the architecture. Suggested shape:

1. **The problem.** An LLM given broker credentials is a liability, not a strategy. The hard
   part is not getting a model to pick a trade — it is making an autonomous system you can
   leave running, audit afterwards, and stop instantly.
2. **What we built.** A persistent, supervised, multi-agent options trading service on
   Alpaca. Five independent agent loops in one asyncio process, each crash-isolated with
   exponential backoff, sharing one clean shutdown path. It has been running continuously
   on a dedicated $100k paper account since <date>.
3. **How it reasons — technical analysis, not news.** A deterministic evidence pack (trend
   from SMA/ADX/RSI, an IV/RV volatility-regime read from the option chain, an earnings-date
   blackout check — no headlines, no sentiment) feeds one Featherless LLM call inside a
   single `StrategistAgent`, which produces a thesis, an invalidation condition, and a
   conviction score. A deterministic **Strategy Matrix** then turns the already-classified
   trend and volatility regime into one of nine defined-risk option structures — the model
   never picks the structure, it only narrates and scores a read the code has already made.
4. **The safety architecture — lead with this, it is the differentiator.**
   - The LLM cannot name an option contract. It emits direction, target delta, DTE window and
     structure; deterministic code selects real contracts from the live chain. Hallucinating
     a strike is structurally impossible.
   - The risk engine has zero LLM imports and cannot be reasoned around: defined-risk only,
     per-trade and portfolio premium caps, DTE and liquidity floors, daily-loss and drawdown
     halts, a kill switch, and idempotent `client_order_id`s.
   - Failures fail closed. A malformed model reply produces no trade, never a guessed one.
     A failed order is recorded as failed and never as a synthetic fill.
   - Exits are deterministic and keep working when the LLM is down or the kill switch is on.
5. **Alpaca infrastructure.** The official Alpaca MCP Server (**v2.3.0**, FastMCP +
   OpenAPI), consumed in-process as a long-lived MCP client over stdio — calendar, account,
   chain snapshots with greeks and IV, multi-leg option orders, positions. Plus a CLI over
   the same modules. Name two specifics that show it is real integration, not a checkbox: the
   server-side toolset allowlist (`ALPACA_TOOLSETS`) that narrows the surface to what we
   actually use (deliberately excluding `news` — this system trades on technical analysis
   only), and a local-cache design that turns the market calendar, account state and open
   positions into single-writer Postgres tables instead of hitting the live API on every
   agent iteration. Say that the order workflow follows Alpaca's own
   `alpaca-trading-paper-trading-mcp` skill: preview, verify paper mode, submit with an
   idempotency key, monitor.
6. **It learns.** Closed trades become short lessons in Postgres, re-injected into
   `StrategistAgent`'s next evidence pack for that symbol.
7. **Results.** Real numbers from the live run: proposals made, trades taken, trades
   declined and by which rule, realised P/L, equity curve.
8. **A screenshot of the decision timeline.** One expanded decision chain says more than a
   paragraph.

Keep it to one page. Cut the architecture diagram before you cut the safety section.

---

## `VIDEO_SCRIPT.md` — ~3 minutes

| Time | Content |
| --- | --- |
| 0:00–0:20 | The hook: "This has been trading options on its own since Tuesday. Here is every decision it made, and every one it refused." Dashboard on screen, live. |
| 0:20–0:50 | The problem: autonomy is easy, auditable autonomy is not. |
| 0:50–1:40 | **Walk one real decision end to end** on the dashboard: evidence (trend + IV/RV regime) → the LLM's thesis and invalidation → the Strategy Matrix verdict → the real contracts the code selected → the risk verdict → the filled order in Alpaca. This is the whole video; give it the time. |
| 1:40–2:10 | Safety: the risk-events feed of declined trades; hit the kill switch live and show new orders stop while an exit still goes through. |
| 2:10–2:35 | Architecture in one breath: five supervised loops, Alpaca MCP, Featherless, Postgres audit trail, deployed and running 24/7. |
| 2:35–3:00 | Results: equity curve, trades, lessons learned. Close on the live dashboard. |

Record the dashboard at 1080p. Show the real Alpaca account at least once — judges want to
see it is not a mock. Do not narrate code.

---

## `SLIDES.md` — 10 slides

Title · The problem · Architecture (five loops) · Technical analysis, not news — the
Strategy Matrix · **The LLM cannot name a contract** · The risk engine · Alpaca MCP
integration (+ the local-cache design) · Live results · It learns · Try it (repo + live URL
+ account ID).

---

## `social_posts.md`

Five posts tagging `@lablabai` and `@AlpacaHQ`. One per angle: the live dashboard, the
"LLM can't name a contract" safety design, the risk-events feed, the MCP integration, the
final results. Each with a screenshot or a short clip.

---

## Repository README

The README a judge opens first. It must contain, above the fold: what it is in two
sentences, a dashboard screenshot, the live URL, how to run it locally in four commands, the
architecture in one diagram, and an explicit safety section. Move the existing deployment
detail below that — it is excellent content but it is not the opening argument.

Also confirm: `.env.example` has names only, no key or account ID is committed anywhere in
history, and the licence and repo visibility are set correctly.

---

## Final checklist

- [ ] Repo public, README rewritten, no secrets in history
- [ ] Service live and healthy; UptimeRobot pinging; kill switch off; `DRY_RUN=false`
- [ ] `WRITEUP.md` exported to PDF, one page
- [ ] Video recorded, uploaded, link works from a private browser window
- [ ] Slides exported to PDF
- [ ] `SUBMISSION_METADATA.md` character counts verified
- [ ] Cover image 16:9
- [ ] 5 social posts published, links collected
- [ ] Paper Account ID in the submission
- [ ] Featherless voucher `ALPACAA26` applied and the integration clearly stated
- [ ] **Submitted well before 4 Sep 15:00 UTC**
- [ ] `options-m flatten` run and confirmed clean before the deadline (Phase 5) — no
      unmonitored open positions at judging time
- [ ] Service left running through judging — do not redeploy after submitting

---

## Traps

- The form takes longer than you think. Do not start it at 14:00 UTC.
- Do not redeploy after submission; a broken live URL during judging costs more than any
  late fix gains.
- Do not overclaim. Every number in the write-up must come from the database. The reference
  submissions claim features their code does not implement, and a judge who opens the repo
  will notice.
- Keep the video under the stated limit and check the audio before recording all three minutes.
