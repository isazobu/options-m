"""Dynamic position sizing: how big this trade should be, given the state.

Deterministic, zero LLM, zero MCP, zero store writes — the same discipline as
:mod:`options_m.risk`, and for the same reason. The contract count is a
safety-critical quantity, so nothing in the agent/LLM layer may reason its way
into a bigger one: :func:`size_position` is a pure function of the plan's
per-contract risk and a :class:`SizingState`, and is therefore trivially
unit-testable.

Three things this module deliberately is *not*:

* **Not a second risk gate.** :mod:`options_m.risk` still evaluates every plan
  independently against the same ``Settings``. Sizing shrinks a trade; risk
  refuses one. A bug here can only ever produce a position smaller than the
  ceiling risk.py enforces, never larger, because
  :func:`size_position` clamps its own risk fraction to
  ``max_premium_pct_per_trade`` before it counts contracts.
* **Not a martingale.** Every scalar moves size *down* after losses and *up*
  after gains. Doubling into a drawdown to "win it back" is precisely how an
  account reaches its halt threshold and stops being able to trade at all; the
  way to stay able to recover is to still be solvent and still be under the
  halt when the next good setup arrives.
* **Not a clock or broker reader.** ``date.today()`` and the broker are the
  caller's problem. Everything time- or account-dependent arrives inside
  :class:`SizingState`, which is why a backtest can size a trade at a past date
  without patching this module.

Every nullable field on :class:`SizingState` means "unknown", and the two kinds
of unknown are handled differently on purpose:

* Unknown *capacity* (equity, buying power) blocks the trade. Approving an
  order because a broker field was unreadable is the dangerous direction.
* Unknown *history* (no high-water mark, no campaign) scales by 1.0. A fresh
  database has genuinely observed no drawdown; refusing to trade until it has
  would mean the service can never place its first order.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from options_m.config import Settings
from options_m.mcp_client import finite_float

if TYPE_CHECKING:
    from options_m.store import Store

_CONTRACT_MULTIPLIER = 100.0

# Options are never marginable: a premium is paid in full, and a defined-risk
# short structure posts its whole width as collateral. Alpaca's headline
# ``buying_power`` is the *margin* figure — 2x equity on a margin account — so
# sizing against it would authorise twice the options exposure the account can
# actually carry. Read the options-specific fields first and fall back only as
# far as cash. ``buying_power`` is deliberately absent from this chain.
_BUYING_POWER_KEYS = (
    "options_buying_power",
    "non_marginable_buying_power",
    "cash",
)

# Collateral is released, not consumed: the shares are already held and already
# paid for, so writing a call against them ties up no additional buying power.
_NO_COLLATERAL_STRATEGIES = frozenset({"covered_call"})


def resolve_options_buying_power(account: dict[str, Any]) -> tuple[float | None, str | None]:
    """The account's *options* buying power, and which field it came from.

    Returns ``(None, None)`` when no field in :data:`_BUYING_POWER_KEYS` carries
    a finite number, which callers must treat as "cannot approve".
    """
    for key in _BUYING_POWER_KEYS:
        value = finite_float(account.get(key))
        if value is not None:
            return value, key
    return None, None


def collateral_per_contract(
    strategy: str, *, max_loss_per_contract: float, strike: float | None
) -> float | None:
    """Buying power one contract of ``strategy`` ties up, in dollars.

    ``None`` means the requirement cannot be computed from what was passed, and
    must block the trade rather than default to something permissive.
    """
    if strategy in _NO_COLLATERAL_STRATEGIES:
        return 0.0
    if strategy == "cash_secured_put":
        # The full assignment cost sits in cash until expiry. max_loss is
        # strike-minus-premium, which understates the collateral actually held.
        return None if strike is None else strike * _CONTRACT_MULTIPLIER
    # Everything else: a debit is paid in full, and a defined-risk credit
    # structure posts (width - credit) per side — which is exactly max_loss.
    return max_loss_per_contract if max_loss_per_contract > 0 else None


class SizingState(BaseModel):
    """Everything :func:`size_position` needs to know about the account now."""

    model_config = ConfigDict(frozen=True)

    equity: float | None
    options_buying_power: float | None
    buying_power_source: str | None
    cash: float | None
    # Alpaca's ``last_equity`` — equity as of the previous close, the daily-loss
    # baseline with no timezone-boundary guessing.
    start_of_day_equity: float | None
    high_water_mark: float | None
    # Equity at the first observation on or after the campaign's start date.
    campaign_start_equity: float | None
    # Sessions left in the campaign, today included. ``None`` means no campaign
    # is configured, which disables horizon pacing entirely rather than
    # implying an unlimited one.
    sessions_remaining: int | None
    is_first_session: bool
    # How far conviction has actually predicted P&L, in [0, 1], and how many
    # closed trades that is measured over. See conviction_reliability: with too
    # few samples this is the configured prior, never 1.0.
    conviction_reliability: float
    conviction_samples: int

    @classmethod
    def from_account(cls, account: dict[str, Any], *, prior: float = 1.0) -> SizingState:
        """State from the broker's account payload alone, with no history.

        The honest answer for the CLI's one-shot ``plan`` command and for a
        first run against an empty store: capacity is real and read from the
        broker, while every history-derived scalar is neutral because nothing
        has been observed yet. :func:`build_sizing_state` is the live path.
        """
        buying_power, source = resolve_options_buying_power(account)
        return cls(
            equity=finite_float(account.get("equity")),
            options_buying_power=buying_power,
            buying_power_source=source,
            cash=finite_float(account.get("cash")),
            start_of_day_equity=finite_float(account.get("last_equity")),
            high_water_mark=None,
            campaign_start_equity=None,
            sessions_remaining=None,
            is_first_session=False,
            # No closed trades observed, so conviction is neither trusted in
            # full nor discarded — the same "unknown history is neutral, not
            # refusing" rule the drawdown scalar follows, with the difference
            # that the neutral answer here is a configured prior rather than 1.0.
            conviction_reliability=prior,
            conviction_samples=0,
        )


class SizingDecision(BaseModel):
    """The contract count, and the complete arithmetic that produced it.

    ``scalars`` and ``caps`` are carried so the rejection detail and the stored
    proposal both explain *why* a trade came out at the size it did. In a system
    whose whole premise is an auditable decision trail, "4 contracts" with no
    derivation is barely better than a guess.
    """

    model_config = ConfigDict(frozen=True)

    qty: int
    risk_fraction: float
    risk_budget: float
    # Which cap produced ``qty``, or which unknown blocked it.
    binding_constraint: str
    scalars: dict[str, float]
    caps: dict[str, int]
    blocked_reason: str | None = None

    @property
    def detail(self) -> dict[str, Any]:
        """Flattened audit trail, for ``Rejection.detail`` and the trace log."""
        return {
            "qty": self.qty,
            "risk_fraction": round(self.risk_fraction, 6),
            "risk_budget": round(self.risk_budget, 2),
            "binding_constraint": self.binding_constraint,
            "scalars": {name: round(value, 4) for name, value in self.scalars.items()},
            "caps": self.caps,
        }


def _taper(loss_fraction: float, halt_fraction: float, floor: float) -> float:
    """1.0 at no loss, tapering linearly to ``floor`` at the halt threshold.

    Beyond the threshold the risk engine's own breaker has already fired, so the
    value there only matters for the audit trail — it stays at the floor rather
    than going negative or to zero.
    """
    if loss_fraction <= 0:
        return 1.0
    if halt_fraction <= 0:
        return floor
    return max(floor, 1.0 - min(1.0, loss_fraction / halt_fraction) * (1.0 - floor))


def drawdown_scalar(state: SizingState, settings: Settings) -> float:
    """Size down as equity falls — the capital-preservation half of the system.

    Two drawdowns are measured against the two breakers in risk.py, and the
    *worse* of them binds: peak-to-now against ``drawdown_halt_pct``, and
    today-versus-previous-close against ``daily_loss_halt_pct``. Over a
    campaign of a few sessions the daily one is usually the binding constraint,
    which is the point — a bad morning shrinks the afternoon's bets while there
    is still an afternoon to trade.
    """
    equity = finite_float(state.equity)
    if equity is None or equity <= 0:
        return 1.0

    scalar = 1.0
    hwm = finite_float(state.high_water_mark)
    if hwm is not None and hwm > 0:
        scalar = min(
            scalar,
            _taper(
                (hwm - equity) / hwm,
                settings.drawdown_halt_pct,
                settings.drawdown_size_floor,
            ),
        )
    start = finite_float(state.start_of_day_equity)
    if start is not None and start > 0:
        scalar = min(
            scalar,
            _taper(
                (start - equity) / start,
                settings.daily_loss_halt_pct,
                settings.drawdown_size_floor,
            ),
        )
    return scalar


def gain_scalar(state: SizingState, settings: Settings) -> float:
    """Size up as the campaign profits — bigger bets funded by realised gains.

    Measured from the campaign's opening equity, not from the high-water mark:
    against the mark a winning account is always at zero gain by definition, so
    it could never step up. Falls back to the previous close when no campaign is
    configured, which turns this into a plain intraday momentum scalar.
    """
    equity = finite_float(state.equity)
    base = finite_float(state.campaign_start_equity)
    if base is None:
        base = finite_float(state.start_of_day_equity)
    if equity is None or base is None or base <= 0:
        return 1.0
    gain = (equity - base) / base
    if gain <= 0:
        return 1.0
    reached = min(1.0, gain / settings.gain_size_reference_pct)
    return 1.0 + reached * (settings.gain_size_cap - 1.0)


def conviction_reliability(
    outcomes: list[tuple[float, float]], settings: Settings
) -> tuple[float, int]:
    """How much this system's stated conviction has actually predicted, and n.

    Sizing by conviction is Kelly-flavoured: bet in proportion to edge. But
    Kelly wants a *calibrated* probability, and what arrives here is a language
    model's self-reported confidence — a number with no prior claim to being
    predictive at all. Measuring it is the difference between sizing on edge and
    sizing on the model's self-assurance.

    Returns Pearson correlation between conviction and realised P&L, clamped to
    ``[0, 1]``, together with the sample size. Negative correlation clamps to
    zero rather than inverting: "high conviction has lost money so far" is a
    reason to stop leaning on the number, not a reason to bet against the
    system's own thesis on a handful of trades.

    Below ``conviction_calibration_min_samples`` the answer is the configured
    prior, not 1.0. A short campaign produces something like eight closed
    trades, at which sample size a correlation is noise — and assuming
    conviction is fully predictive on no evidence is the aggressive direction.
    """
    usable: list[tuple[float, float]] = []
    for raw_conviction, raw_pnl in outcomes:
        conviction = finite_float(raw_conviction)
        pnl = finite_float(raw_pnl)
        if conviction is not None and pnl is not None:
            usable.append((conviction, pnl))
    n = len(usable)
    if n < settings.conviction_calibration_min_samples:
        return settings.conviction_reliability_prior, n

    mean_x = sum(x for x, _ in usable) / n
    mean_y = sum(y for _, y in usable) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in usable)
    var_x = sum((x - mean_x) ** 2 for x, _ in usable)
    var_y = sum((y - mean_y) ** 2 for _, y in usable)
    # Every proposal carried the same conviction, or every trade the same P&L.
    # Nothing to learn either way, so fall back to the prior rather than reading
    # a degenerate result as "conviction is worthless".
    #
    # Tested on the standard deviation against a floor, not on the variance
    # against zero: three identical 0.8s have a mean of 0.8000000000000002 in
    # binary float, which leaves a variance around 1e-31 rather than 0. That
    # passes a `> 0` guard, and the correlation computed from it is the ratio of
    # two pieces of rounding noise — here it came out negative and clamped to
    # 0.0, which would have silently flattened every trade to one size.
    if _degenerate(var_x, n) or _degenerate(var_y, n):
        return settings.conviction_reliability_prior, n
    correlation = cov / math.sqrt(var_x * var_y)
    return min(1.0, max(0.0, correlation)), n


# Both series live on a scale of order 1 — conviction in [0, 1], pnl_pct roughly
# in [-1, 2] — so a standard deviation this small is rounding noise, never data.
_DEGENERATE_STDDEV = 1e-9


def _degenerate(variance: float, n: int) -> bool:
    """Whether a series has no real spread, float noise notwithstanding."""
    return variance <= 0 or math.sqrt(variance / n) < _DEGENERATE_STDDEV


def conviction_scalar(conviction: float, settings: Settings, reliability: float = 1.0) -> float:
    """Map the live conviction band onto a size multiplier.

    Conviction below ``conviction_floor`` never reaches sizing — the matrix has
    already forced "hold" — so the band that actually occurs is
    ``[conviction_floor, 1.0]``, and mapping *that* onto the multiplier range is
    what makes the knob do anything. Scaling from 0.0 instead would put every
    real proposal in the top sliver of the range and flatten the distinction
    between a marginal setup and a strong one.

    ``reliability`` then shrinks the result toward 1.0 — textbook shrinkage
    toward the prior. At 1.0 conviction is trusted in full; at 0.0 it is ignored
    and every trade is sized the same, which is the right answer when the
    measured relationship between conviction and P&L is nil. See
    :func:`conviction_reliability`.
    """
    low = settings.conviction_size_min_mult
    high = settings.conviction_size_max_mult
    span = 1.0 - settings.conviction_floor
    if span <= 0:
        raw = high
    else:
        reached = min(1.0, max(0.0, (conviction - settings.conviction_floor) / span))
        raw = low + reached * (high - low)
    trust = min(1.0, max(0.0, reliability))
    return 1.0 + trust * (raw - 1.0)


def horizon_scalar(state: SizingState, settings: Settings) -> float:
    """Pace the campaign: front-load the first session, stop before the last.

    Returns exactly 0.0 once too few sessions remain, which
    :func:`size_position` reports as ``campaign_horizon_closed`` rather than as
    an ordinary zero quantity. A position opened with no session left to work in
    pays the bid/ask twice for whatever drift happens in between.
    """
    remaining = state.sessions_remaining
    if remaining is None:
        return 1.0
    if remaining <= settings.campaign_min_sessions_to_hold:
        return 0.0
    if state.is_first_session:
        return settings.campaign_front_load_mult
    return 1.0


def _blocked(reason: str, scalars: dict[str, float]) -> SizingDecision:
    return SizingDecision(
        qty=0,
        risk_fraction=0.0,
        risk_budget=0.0,
        binding_constraint=reason,
        scalars=scalars,
        caps={},
        blocked_reason=reason,
    )


def size_position(
    *,
    strategy: str,
    conviction: float,
    max_loss_per_contract: float,
    state: SizingState,
    settings: Settings,
    strike: float | None = None,
) -> SizingDecision:
    """How many contracts to trade, and the derivation behind the number.

    ``max_loss_per_contract`` is per-contract dollars — the caller multiplies
    the returned ``qty`` back in when it fills the plan's totals.
    """
    scalars = {
        "drawdown": drawdown_scalar(state, settings),
        "gain": gain_scalar(state, settings),
        "conviction": conviction_scalar(conviction, settings, state.conviction_reliability),
        "horizon": horizon_scalar(state, settings),
    }

    if scalars["horizon"] <= 0:
        return _blocked("campaign_horizon_closed", scalars)

    equity = finite_float(state.equity)
    if equity is None or equity <= 0:
        return _blocked("no_account_equity", scalars)
    if max_loss_per_contract <= 0:
        # The builders compute a finite, positive max_loss before they get here
        # or they return a Rejection instead, so this is a guard against a
        # future caller, not a path production takes.
        return _blocked("non_positive_max_loss", scalars)

    risk_fraction = settings.base_risk_pct_per_trade
    for scalar in scalars.values():
        risk_fraction *= scalar
    # The hard per-trade ceiling risk.py enforces independently. Clamping here
    # rather than trusting the scalars to stay inside it is what keeps the two
    # layers agreeing: a plan sized above the ceiling would be built, priced and
    # then rejected downstream, burning broker calls to produce nothing.
    risk_fraction = min(risk_fraction, settings.max_premium_pct_per_trade)
    risk_budget = risk_fraction * equity

    caps = {"risk_budget": math.floor(risk_budget / max_loss_per_contract)}

    collateral = collateral_per_contract(
        strategy, max_loss_per_contract=max_loss_per_contract, strike=strike
    )
    if collateral is None:
        return _blocked("unknown_collateral_requirement", scalars)
    if collateral > 0:
        buying_power = finite_float(state.options_buying_power)
        if buying_power is None:
            return _blocked("unknown_buying_power", scalars)
        usable = max(0.0, buying_power) * settings.buying_power_utilization_cap
        caps["buying_power"] = math.floor(usable / collateral)

    # Kept alongside the collateral cap above rather than folded into it: the
    # buying-power chain can resolve to a field other than cash, and an
    # assignment settles in cash specifically. ``strike`` is known to be set
    # here — collateral_per_contract returned None for a put without one, and
    # that already blocked above.
    if strategy == "cash_secured_put" and strike is not None:
        cash = finite_float(state.cash)
        if cash is None:
            return _blocked("unknown_cash", scalars)
        caps["cash"] = math.floor(max(0.0, cash) / (strike * _CONTRACT_MULTIPLIER))

    binding = min(caps, key=lambda name: caps[name])
    qty = max(0, caps[binding])
    return SizingDecision(
        qty=qty,
        risk_fraction=risk_fraction,
        risk_budget=risk_budget,
        binding_constraint=binding,
        scalars=scalars,
        caps=caps,
        blocked_reason=None if qty > 0 else f"zero_quantity:{binding}",
    )


async def build_sizing_state(
    account: dict[str, Any],
    *,
    store: Store,
    settings: Settings,
    now: datetime | None = None,
) -> SizingState:
    """The live :class:`SizingState`: broker capacity plus observed history."""
    base = SizingState.from_account(account, prior=settings.conviction_reliability_prior)

    equity_history = await store.recent_equity(limit=500)
    observed = [
        value for row in equity_history if (value := finite_float(row.get("equity"))) is not None
    ]
    high_water_mark = max(observed) if observed else None

    campaign_start_equity: float | None = None
    sessions_remaining: int | None = None
    is_first_session = False

    start_date = settings.campaign_start_date
    if start_date is not None:
        today = store.session_day(now or datetime.now(UTC))
        elapsed = await store.sessions_between(start_date, today)
        # A cold calendar counts zero sessions. That cannot coexist with live
        # trading — session.current reads a missing calendar row as closed, so
        # every agent has already short-circuited — which leaves only the
        # legitimate case: a campaign whose start date has not arrived yet.
        sessions_elapsed = max(1, elapsed)
        sessions_remaining = max(0, settings.campaign_days - sessions_elapsed + 1)
        is_first_session = sessions_elapsed <= 1
        campaign_start_equity = _equity_at_campaign_start(
            equity_history, start_date, store.session_day
        )

    outcomes = await store.conviction_outcomes()
    reliability, samples = conviction_reliability(
        [(row["conviction"], row["pnl_pct"]) for row in outcomes], settings
    )

    return base.model_copy(
        update={
            "high_water_mark": high_water_mark,
            "campaign_start_equity": campaign_start_equity,
            "sessions_remaining": sessions_remaining,
            "is_first_session": is_first_session,
            "conviction_reliability": reliability,
            "conviction_samples": samples,
        }
    )


def _equity_at_campaign_start(
    equity_history: list[dict[str, Any]],
    start_date: date,
    session_day: Callable[[datetime], date],
) -> float | None:
    """The oldest observed equity on or after ``start_date``.

    ``recent_equity`` returns newest-first, so the *last* qualifying row is the
    campaign's opening mark. Rows predating the campaign are ignored rather than
    used as an approximation: a stale baseline would misreport the campaign's
    gain, and misreporting it upward is what would size a trade up.
    """
    opening: float | None = None
    for row in equity_history:
        timestamp = row.get("ts")
        if not isinstance(timestamp, datetime) or session_day(timestamp) < start_date:
            continue
        value = finite_float(row.get("equity"))
        if value is not None:
            opening = value
    return opening
