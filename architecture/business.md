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

## What are options? (Quick primer)

An option is a contract that gives the right to buy or sell a stock at a
specific price before a specific date. Instead of buying a stock for $100,
you might pay $5 for the *option* to buy it at $100 within the next 30 days.

options-m trades **multi-leg structures** — combinations of two or four option
contracts designed so that the maximum possible loss is defined upfront before
the trade is placed. The system never places a trade where the loss is
theoretically unlimited.

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

### 1. Market Watcher (MarketPulseAgent)
**Runs every 60 seconds. Never trades.**

Collects data on every stock in the universe:
- What is the stock's price and recent trend?
- Is implied volatility (the option market's fear gauge) expensive or cheap
  relative to how much the stock has actually been moving?
- Is there an earnings report coming up? (If so, that stock is off-limits.)
- What is the account's current cash position?

Ranks all 10 stocks by "how tradeable does this look right now" and saves
that ranking for the Strategist.

### 2. Position Tracker (PositionManagerAgent)
**Runs every 60 seconds. Never trades.**

Watches all open positions in the brokerage account and updates their
current profit/loss every minute. Acts as the source of truth for "what do
we currently own and what is it worth right now?"

Also watches for exit conditions and creates a close request when a position
hits its profit target, stop loss, or has been held too long.

### 3. Strategist (StrategistAgent)
**Runs every 5 minutes. Makes decisions but does not touch the broker.**

The decision-maker. Every 5 minutes it:
1. Picks the highest-ranked stock from the Market Watcher's list.
2. Reads the full data package for that stock.
3. Asks an AI (via a structured prompt) to read the market regime and state
   its conviction level.
4. Runs a deterministic decision table ("if the trend is up and options are
   expensive, sell a put credit spread") to pick a strategy.
5. Records the decision as a "proposal" — either a trade to execute or a
   reasoned hold.

The AI's job is narrow: assess the regime and express conviction. The actual
strategy selection is rule-based code, not an AI free-form output.

### 4. Trader (ExecutionAgent)
**Runs every 30 seconds. The only agent that touches the broker.**

Takes approved proposals from the Strategist, does the work to turn them into
real orders:
1. Gets the current stock price.
2. Pulls the live options chain (all available contracts and their prices).
3. Selects the specific contracts that best match the strategy's target
   (e.g. "find the put closest to a 25% delta expiring in 21–38 days").
4. Calculates the entry price and worst-case loss.
5. Runs a risk check (see Safety Nets below).
6. If everything passes: places the order with the broker.

It also periodically checks the status of submitted orders and updates the
record when they are filled, rejected, or cancelled by the broker.

### 5. Reviewer (ReflectionAgent)
**Runs every 60 minutes. Never trades.**

Looks back at completed trades and past hold/reject decisions and asks the AI:
- "That trade closed. What can we learn from what happened vs. what we expected?"
- "We held on that stock. Was that the right call in hindsight?"

Saves those lessons so the Strategist can factor them in the next time it
evaluates the same stock.

---

## How a Trade Gets Born: End-to-End

```
Every 60 s: Market Watcher scans all 10 stocks
            → ranks them by tradeable signal
            → saves ranking + data to database

Every 5 m:  Strategist picks the top-ranked stock
            → reads its data package
            → calls AI: "What regime are we in? How confident are you?"
            → runs strategy table: (trend + IV regime) → strategy name
            → if conviction ≥ 55%: saves a PENDING proposal
            → if conviction < 55% or no clear signal: saves a NO_ACTION record

Every 30 s: Trader checks for PENDING proposals
            → fetches live options chain from broker
            → finds the best-fit contracts
            → computes price and max loss
            → runs safety checks (see below)
            → if all pass: places order → proposal becomes SUBMITTED
            → if any check fails: proposal becomes REJECTED (reason recorded)

Every 60 s: Position Tracker checks open trades
            → profit target hit? → creates a close proposal
            → stop loss hit? → creates a close proposal
            → held too long? → creates a close proposal
```

Every step is recorded in the database. You can trace any trade — or any
decision *not* to trade — back through each stage.

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
