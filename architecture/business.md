# options-m — Product Overview

## What is options-m?

options-m is an autonomous options-trading agent. It runs 24/7, watches a fixed
list of 10 stocks, decides when conditions are right to place an options trade,
submits that trade to a brokerage account, and tracks the result — all without
human intervention.

The system is designed to be **fully auditable**: every decision it makes
(including the ones it decides *not* to act on) is stored with its complete
reasoning, so any outcome can be traced back step by step.

---

## The 10 stocks it watches

```
SPY  QQQ  IWM  AAPL  MSFT  NVDA  AMD  TSLA  META  GOOGL
```

These are the "universe" — a fixed list configured at deployment. The system
evaluates all 10 every minute and picks the best-looking opportunity each cycle.

---

## The Five Agents

The system is built from five specialised workers ("agents") that run
simultaneously. Think of them as team members with distinct roles.

A note on **AI vs. deterministic logic**: only two agents use AI at all, and
even then only for a narrow, bounded task. Everything else — data collection,
ranking, contract selection, risk checks, exit conditions — is pure rule-based
code that produces the same output given the same input, every time.

---

### 1. Market Watcher (MarketPulseAgent)
**Cadence: every 60 seconds. Never trades. 100% deterministic — no AI.**

#### What it does
Collects fresh data on every stock in the universe and computes a
"tradeability score" for each one. Saves the ranked list and the full data
package so the Strategist can read it without going to the broker itself.

#### Conditions — when it does full work
- The market is open (NYSE trading hours, checked against a local calendar cache).

#### Conditions — when it skips the data collection step
- The market is closed: it still updates the account snapshot (equity, cash,
  buying power) but does not collect options data or rank candidates.

#### Scoring formula (deterministic)
Each stock gets a score from three signals — all pure arithmetic, no AI:

| Signal | How it's measured |
|---|---|
| RSI extremity | How far the 14-day RSI is from neutral (50). A stock at RSI 70 scores higher than one at RSI 52. |
| Realised volatility | How much the stock has actually been moving over the last 20 days. More movement = more premium available to trade. |
| IV/RV edge | How much the options market's implied volatility exceeds the stock's recent actual volatility. The bigger the gap, the more "expensive" options are. |

Stocks with upcoming earnings are automatically scored 0 and sink to the
bottom of the list regardless of other signals.

---

### 2. Position Tracker (PositionManagerAgent)
**Cadence: every 60 seconds. Never opens trades. 100% deterministic — no AI.**

#### What it does
Fetches all open positions from the broker, groups option legs by the
underlying stock they belong to, and saves a current snapshot of each
position including live profit/loss.

Also evaluates every open position against exit rules and creates a "close
this position" instruction when one is triggered.

#### Conditions — when it runs normally
- Always — this agent is never gated by market hours or kill switch, because
  monitoring positions must work even when new trades are blocked.

#### Exit conditions it checks (deterministic)
| Condition | Threshold | What happens |
|---|---|---|
| Profit target reached | Position is up 50% from entry | Creates a close instruction |
| Stop loss hit | Position is down 50% from entry | Creates a close instruction |
| Time stop | Position has been open 30+ days | Creates a close instruction |

When any condition is met, a close instruction is written to the database.
The Trader picks it up and submits the actual closing order to the broker.

---

### 3. Strategist (StrategistAgent)
**Cadence: every 5 minutes. Makes decisions but never touches the broker.**
**Uses AI for one narrow task; everything else is deterministic.**

#### What it does
The decision-maker. Picks the best-ranked stock, reads its data, asks the AI
to assess the market regime, then runs a rule table to pick a strategy.

#### Conditions — when it skips the entire cycle
The Strategist checks these in order and stops immediately if any is true:

| Condition | What it records |
|---|---|
| Market is closed | `skipped: market_closed` |
| Kill switch is on | `skipped: kill_switch` |
| AI is not configured (no API key) | `skipped: llm_not_configured` |
| Daily AI budget is exhausted | `skipped: llm_budget_exhausted` |
| No stock passes the candidate filters (see below) | `skipped: no_candidate` |
| No fresh data exists for the selected stock | `skipped: no_evidence_cache` |
| Data for the selected stock is stale (>2 min old) | `skipped: stale_evidence` |

> **Important:** the kill switch only blocks new trade proposals. The close
> evaluation (checking whether open positions need to exit) runs regardless —
> exits must work even when new entries are frozen.

#### Candidate filtering (deterministic)
Before selecting a stock to evaluate, the Strategist silently skips any stock that:
- Already has an open position in the account
- Already has a pending trade proposal waiting to be executed
- Is inside its earnings blackout window

#### The AI step — what it is and is not
The Strategist makes **one AI call per cycle**, with a structured prompt that
includes the stock's data package. The AI is asked to return three things only:

- **Thesis**: why does this setup look tradeable?
- **Invalidation**: what would make this thesis wrong?
- **Conviction**: a number from 0 to 1

The AI does **not** pick the strategy. It does **not** select contracts. It
does **not** decide the size. Its only operational role is the conviction
number — if that number is below 0.55, the Strategist records a hold and stops.

#### Strategy selection (deterministic)
Once conviction passes the threshold, a fixed rule table picks the strategy
based on two signals derived from the data (not from the AI):

| Market trend | Options expensive (IV > realized vol) | Options cheap |
|---|---|---|
| Trending up | Sell a put spread | Buy a call spread |
| Going sideways | Sell an iron condor / butterfly | Buy a strangle |
| Trending down | Sell a call spread | Buy a put spread |

If the account's options trading level is below 3, credit structures are
downgraded to simple long calls or long puts. If no level-appropriate
structure exists, the Strategist records a hold.

The outcome is either a **pending proposal** (a specific strategy to execute)
or a **no-action record** (a hold, with the reason stored).

---

### 4. Trader (ExecutionAgent)
**Cadence: every 30 seconds. The only agent that touches the broker.**
**100% deterministic — no AI.**

#### What it does
Picks up pending proposals and turns them into real broker orders. Also
continuously checks the status of submitted orders and updates the record
when the broker fills, rejects, or cancels them.

#### Conditions — when it skips entirely
- Kill switch is on: stops immediately and records the event.

#### Per-proposal conditions — what can block a trade
For each pending proposal, the Trader works through these steps in order.
A failure at any step rejects the proposal with a recorded reason:

| Step | What can go wrong | Result |
|---|---|---|
| Parse the strategy intent | Malformed proposal from database | `rejected: invalid_intent` |
| Get current stock price | Price unavailable from broker | `rejected: no_spot_price` |
| Build the order (select contracts) | No contracts in target date/delta range | `rejected: no_contracts_in_window` |
| Build the order | Selected contracts have too-wide bid/ask spread | `rejected: wide_spread` |
| Build the order | Not enough premium to justify the risk | `rejected: thin_credit` |
| Build the order | Position would collect too much of the spread width | `rejected: credit_too_rich` |
| Risk check | Trade would use more than 2% of account equity | `rejected: max_premium_exceeded` |
| Risk check | Total open exposure would exceed 15% of equity | `rejected: total_premium_exceeded` |
| Risk check | Already have 5 open positions | `rejected: max_concurrent_positions` |
| Risk check | Already have a position in this stock | `rejected: max_positions_per_underlying` |
| Risk check | Account lost more than 3% today | `rejected: daily_loss_halt` |
| Risk check | Account is down more than 8% from peak | `rejected: drawdown_halt` |
| Risk check | Less than 15 minutes to market close | `rejected: end_of_day_blackout` |
| Risk check | Dry run mode is on | Proposal marked `dry_run_approved`, no order sent |

If all checks pass, the order is sent to the broker.

#### After submission — order tracking
Every 30 seconds, the Trader also polls any submitted orders that haven't
settled yet. If the broker marks an order as rejected or cancelled, the
Trader records the broker's reason and marks the original proposal as
`broker_rejected`.

---

### 5. Reviewer (ReflectionAgent)
**Cadence: every 60 minutes. Never trades.**
**Uses AI — one call per filled trade or unreviewed hold/rejection.**

#### What it does
Looks back at two things: trades that filled, and proposals where the system
decided to hold or got rejected. Asks the AI to write a 1–2 sentence lesson
for each.

#### Conditions — when it runs
- Always — not gated by market hours or kill switch.

#### What the AI is asked (two separate passes)

**Pass A — closed trades:**
For each filled order not yet reviewed, the AI receives: what contracts were
traded, at what price, and what the outcome was. It is asked: "What can we
learn from this?"

**Pass B — holds and rejections:**
For each recent hold or rejection not yet reviewed, the AI receives: the
stock, the thesis, the conviction level, and why it was held or rejected. It
is asked: "Was this the right call? Was it a miss or a save?"

The lesson is stored and automatically included in the data package the next
time the Strategist evaluates the same stock. This is the only feedback loop
from past decisions back into future ones.

If the AI call fails for any reason, that entry is skipped and tried again
next hour. An AI failure here never stops any other agent.

---

## How a Trade Gets Born: End-to-End

```
Every 60 s  Market Watcher
            [deterministic]
            ├── market open?
            │     NO  → update account snapshot only, stop
            │     YES → collect data for all 10 stocks
            │           score each one (RSI + volatility + IV edge)
            │           earnings coming up? → score = 0
            └──────────► save ranked list to database

Every 5 m   Strategist
            ├── market open?          NO  → stop (record: market_closed)
            ├── kill switch on?       YES → stop (record: kill_switch)
            ├── AI configured?        NO  → stop (record: llm_not_configured)
            ├── AI budget left?       NO  → stop (record: llm_budget_exhausted)
            ├── any valid candidate?  NO  → stop (record: no_candidate)
            │     (skips: already have position, already pending, earnings blackout)
            ├── fresh data exists?    NO  → stop (record: no_evidence_cache / stale)
            │
            ├── [AI] ask: "What regime? How confident?"
            │     AI returns: thesis, invalidation, conviction (0–1)
            │     AI fails? → stop (record: llm_failed), no crash
            │
            ├── conviction ≥ 55%?     NO  → stop (record: no_action / hold)
            │
            ├── [deterministic] run strategy table → pick structure
            │     account level too low for that structure? → downgrade or hold
            │
            └──────────► save PENDING proposal to database

Every 30 s  Trader
            ├── kill switch on? → stop
            │
            ├── for each PENDING proposal:
            │     ├── parse valid?              NO  → rejected: invalid_intent
            │     ├── action = close?           YES → build close order from positions cache
            │     ├── get live stock price      FAIL → rejected: no_spot_price
            │     ├── fetch options chain from broker
            │     ├── select best-fit contracts
            │     │     no contracts in range?  → rejected: no_contracts_in_window
            │     │     spread too wide?        → rejected: wide_spread
            │     │     credit too thin?        → rejected: thin_credit
            │     │     credit too rich?        → rejected: credit_too_rich
            │     ├── risk checks [deterministic]
            │     │     trade > 2% of equity?   → rejected: max_premium_exceeded
            │     │     total > 15% of equity?  → rejected: total_premium_exceeded
            │     │     5 positions already?    → rejected: max_concurrent_positions
            │     │     already in this stock?  → rejected: max_positions_per_underlying
            │     │     down 3% today?          → rejected: daily_loss_halt
            │     │     down 8% from peak?      → rejected: drawdown_halt
            │     │     <15 min to close?       → rejected: end_of_day_blackout
            │     ├── dry run on?               YES → dry_run_approved (no order sent)
            │     └──────────► place order with broker → SUBMITTED
            │
            └── check submitted orders with broker
                  broker rejected/cancelled? → record reason as broker_rejected

Every 60 s  Position Tracker [deterministic, always runs]
            └── for each open position:
                  up 50%?       → create close proposal
                  down 50%?     → create close proposal
                  open 30+ days? → create close proposal
                  (Trader picks up the close proposal on its next cycle)

Every 60 m  Reviewer
            ├── [AI] for each filled order not yet reviewed:
            │         "What can we learn from this trade?"
            │         → save lesson, linked to this stock
            └── [AI] for each hold/rejection not yet reviewed:
                      "Was this the right call — miss or save?"
                      → save lesson, linked to this stock
                      (lesson appears in Strategist's data package next time)
```

Every step is recorded in the database with its outcome and reason. You can
trace any trade — or any decision *not* to trade — back through each stage.

---

## The Strategy Decision Table

The Strategist maps two signals to a strategy:

| Market trend | Options are expensive | Options are cheap |
|---|---|---|
| Trending up | Sell a put spread (collect premium) | Buy a call spread (pay for upside) |
| Going sideways | Sell an iron condor/butterfly (collect from both sides) | Buy a strangle (bet on a big move) |
| Trending down | Sell a call spread (collect premium) | Buy a put spread (pay for downside) |

**"Options are expensive"** means the market is pricing in more volatility
than the stock has actually delivered recently. Selling expensive options is
the core edge of a short-volatility strategy.

**"Options are cheap"** means the option market is calm but the trend is
strong, so buying directional exposure makes more sense than selling premium.

---

## Safety Nets

Multiple independent layers prevent a bad trade from going through:

| Safety net | What it does |
|---|---|
| **Earnings blackout** | If a company has an earnings report in the next ~7 days, it is completely excluded. Options behave unpredictably around earnings. |
| **Maximum loss per trade** | Each trade may risk at most 2% of the account's total equity. |
| **Total exposure cap** | All open trades together may not risk more than 15% of equity. |
| **Position limits** | No more than 5 open trades at once. No more than 1 trade per stock. |
| **Liquidity filter** | If the options are too thinly traded (wide bid/ask spread or low open interest), the trade is refused. |
| **Time filter** | Options must expire 7–45 days from today. No same-day or very long-dated positions. |
| **Daily loss halt** | If the account has lost more than 3% today, no new trades are opened. |
| **Drawdown halt** | If the account is down more than 8% from its peak, no new trades are opened. |
| **Kill switch** | A single toggle (available via the dashboard or an API call) that instantly stops all new orders. Existing positions are unaffected until exit conditions trigger separately. |
| **Dry run mode** | When enabled (the default), the system goes through every step including order construction and risk checks, but never submits to the broker. Used for testing and monitoring. |
| **Defined-risk only** | Every structure must have a finite, calculated maximum loss before it is allowed. A trade where the loss is theoretically unlimited is rejected at two independent layers. |
| **AI conviction floor** | The AI must express at least 55% confidence in its regime assessment. Below that, the Strategist holds regardless of what the strategy table says. |

---

## What Gets Stored

For every decision the system makes — trade or no-trade — the database
records:

- The full market data snapshot that was used
- The AI's regime assessment and stated conviction
- Which strategy was selected and why (the rule that fired)
- The actual contracts selected (if a trade was pursued)
- The risk check result and which rules fired
- The order sent to the broker and its outcome
- Any post-trade lessons from the Reviewer

This means every outcome is explainable after the fact, and backtesting can
replay any past period through the same decision logic.

---

## Current Status

The system runs end-to-end in dry-run mode by default. All 9 strategy types
are fully implemented. The key areas still being refined:

- **IV signal quality**: The "options are expensive" signal needs a neutral
  band — currently anything below the threshold is treated as "cheap" even
  when the market is fairly priced. This causes the system to prefer
  long-premium structures more often than is optimal.

- **Exit management**: The position tracker monitors P&L and triggers closes,
  but the logic for what constitutes the right exit (beyond simple
  profit/stop targets) is still basic.

- **Allocation**: When multiple stocks all look good simultaneously, the
  system currently fills slots in the order the universe list is evaluated
  rather than prioritising by signal strength.
