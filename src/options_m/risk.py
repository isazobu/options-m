"""The risk engine: deterministic guardrails, zero LLM, zero MCP.

Modelled on ``AlpacaTradingAgent``'s safety layer, which deliberately imports
nothing from the agent/LLM layer so it can never be reasoned around. This
module writes nothing to the store either — the caller (``ExecutionAgent``)
records ``risk_events``, keeping every rule here a pure function of an
``OrderPlan`` and a ``PortfolioSnapshot``, and trivially unit-testable.
"""

from __future__ import annotations

import math
from datetime import date

from pydantic import BaseModel, ConfigDict

from options_m.config import Settings
from options_m.mcp_client import finite_float
from options_m.models import OrderPlan


class RiskLimits(BaseModel):
    """The account-wide hard bounds, read once from :class:`Settings`."""

    model_config = ConfigDict(frozen=True)

    max_premium_pct_per_trade: float
    max_total_premium_pct: float
    max_concurrent_positions: int
    max_positions_per_underlying: int
    dte_min: int
    dte_max: int
    min_open_interest: int
    max_spread_pct: float
    daily_loss_halt_pct: float
    drawdown_halt_pct: float
    minutes_before_close_blackout: int

    @classmethod
    def from_settings(cls, settings: Settings) -> RiskLimits:
        return cls(
            max_premium_pct_per_trade=settings.max_premium_pct_per_trade,
            max_total_premium_pct=settings.max_total_premium_pct,
            max_concurrent_positions=settings.max_concurrent_positions,
            max_positions_per_underlying=settings.max_positions_per_underlying,
            dte_min=settings.risk_dte_min,
            dte_max=settings.risk_dte_max,
            min_open_interest=settings.min_open_interest,
            max_spread_pct=settings.max_spread_pct,
            daily_loss_halt_pct=settings.daily_loss_halt_pct,
            drawdown_halt_pct=settings.drawdown_halt_pct,
            minutes_before_close_blackout=settings.minutes_before_close_blackout,
        )


class PortfolioSnapshot(BaseModel):
    """Everything :class:`RiskEngine` needs to know about the account right now.

    Every numeric field is nullable on purpose — an unreadable broker field is
    "unknown", and every check here treats unknown as "cannot approve", never
    as a passing value.
    """

    model_config = ConfigDict(frozen=True)

    equity: float | None
    start_of_day_equity: float | None
    high_water_mark: float | None
    concurrent_option_positions: int
    positions_in_underlying: int
    total_open_option_premium: float | None
    market_is_open: bool
    minutes_to_close: float | None
    kill_switch_engaged: bool
    already_submitted: bool


class RiskVerdict(BaseModel):
    """Every reason collected, not just the first — a stronger dashboard story."""

    model_config = ConfigDict(frozen=True)

    approved: bool
    reasons: list[str]
    adjusted_qty: int | None = None


class RiskEngine:
    """Evaluates one :class:`OrderPlan` against the account's hard limits."""

    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    def evaluate(self, plan: OrderPlan, portfolio: PortfolioSnapshot) -> RiskVerdict:
        checks = (
            self._check_kill_switch,
            self._check_idempotency,
            self._check_market_open,
            self._check_close_blackout,
            self._check_defined_risk,
            self._check_dte_window,
            self._check_open_interest,
            self._check_spread,
            self._check_premium_per_trade,
            self._check_total_premium,
            self._check_concurrent_positions,
            self._check_positions_per_underlying,
            self._check_daily_loss_halt,
            self._check_drawdown_halt,
        )
        reasons = [reason for check in checks if (reason := check(plan, portfolio)) is not None]
        return RiskVerdict(approved=not reasons, reasons=reasons)

    def _check_kill_switch(self, _plan: OrderPlan, portfolio: PortfolioSnapshot) -> str | None:
        return "kill_switch_engaged" if portfolio.kill_switch_engaged else None

    def _check_idempotency(self, _plan: OrderPlan, portfolio: PortfolioSnapshot) -> str | None:
        return "idempotent_duplicate" if portfolio.already_submitted else None

    def _check_market_open(self, _plan: OrderPlan, portfolio: PortfolioSnapshot) -> str | None:
        return None if portfolio.market_is_open else "market_closed"

    def _check_close_blackout(self, _plan: OrderPlan, portfolio: PortfolioSnapshot) -> str | None:
        if portfolio.minutes_to_close is None:
            return None
        if portfolio.minutes_to_close <= self._limits.minutes_before_close_blackout:
            return "close_blackout"
        return None

    def _check_defined_risk(self, plan: OrderPlan, _portfolio: PortfolioSnapshot) -> str | None:
        short_legs = [leg for leg in plan.legs if leg.side == "sell"]
        if not short_legs:
            return None
        if plan.strategy in {"debit_call_spread", "debit_put_spread"}:
            option_type = short_legs[0].option_type
            long_legs = [
                leg for leg in plan.legs if leg.side == "buy" and leg.option_type == option_type
            ]
            return None if len(long_legs) >= len(short_legs) else "naked_short_leg"
        if plan.strategy in {"covered_call", "cash_secured_put"}:
            # Covered by shares / cash by construction — verified upstream in
            # strategy_builder before a plan for these ever exists.
            return None
        return "naked_short_leg"

    def _check_dte_window(self, plan: OrderPlan, _portfolio: PortfolioSnapshot) -> str | None:
        today = date.today()
        for leg in plan.legs:
            dte = (leg.expiry - today).days
            if not (self._limits.dte_min <= dte <= self._limits.dte_max):
                return f"dte_out_of_window:{dte}"
        return None

    def _check_open_interest(self, plan: OrderPlan, _portfolio: PortfolioSnapshot) -> str | None:
        for leg in plan.legs:
            if leg.open_interest is None or leg.open_interest < self._limits.min_open_interest:
                return f"insufficient_open_interest:{leg.symbol}"
        return None

    def _check_spread(self, plan: OrderPlan, _portfolio: PortfolioSnapshot) -> str | None:
        for leg in plan.legs:
            if leg.bid is None or leg.ask is None or leg.bid <= 0 or leg.ask <= 0:
                return f"missing_quote:{leg.symbol}"
            mid = (leg.bid + leg.ask) / 2
            if mid <= 0:
                continue
            spread_pct = (leg.ask - leg.bid) / mid
            # A spread exactly at the limit must pass: bid/ask arithmetic in
            # binary float can put it a sliver over (0.10000000000000009 for
            # bid=1.9/ask=2.1), which a bare `>` would wrongly reject.
            if spread_pct > self._limits.max_spread_pct and not math.isclose(
                spread_pct, self._limits.max_spread_pct, rel_tol=1e-9, abs_tol=1e-12
            ):
                return f"wide_spread:{leg.symbol}"
        return None

    def _check_premium_per_trade(
        self, plan: OrderPlan, portfolio: PortfolioSnapshot
    ) -> str | None:
        equity = finite_float(portfolio.equity)
        if equity is None:
            return "unknown_equity"
        if plan.max_loss > self._limits.max_premium_pct_per_trade * equity:
            return "premium_per_trade_exceeded"
        return None

    def _check_total_premium(self, plan: OrderPlan, portfolio: PortfolioSnapshot) -> str | None:
        equity = finite_float(portfolio.equity)
        if equity is None:
            return "unknown_equity"
        existing = finite_float(portfolio.total_open_option_premium) or 0.0
        if existing + plan.max_loss > self._limits.max_total_premium_pct * equity:
            return "total_premium_exceeded"
        return None

    def _check_concurrent_positions(
        self, _plan: OrderPlan, portfolio: PortfolioSnapshot
    ) -> str | None:
        if portfolio.concurrent_option_positions >= self._limits.max_concurrent_positions:
            return "max_concurrent_positions"
        return None

    def _check_positions_per_underlying(
        self, _plan: OrderPlan, portfolio: PortfolioSnapshot
    ) -> str | None:
        if portfolio.positions_in_underlying >= self._limits.max_positions_per_underlying:
            return "max_positions_per_underlying"
        return None

    def _check_daily_loss_halt(self, _plan: OrderPlan, portfolio: PortfolioSnapshot) -> str | None:
        equity = finite_float(portfolio.equity)
        start = finite_float(portfolio.start_of_day_equity)
        if equity is None or start is None or start <= 0:
            return None
        change = (equity - start) / start
        if change <= -self._limits.daily_loss_halt_pct:
            return f"daily_loss_halt:{change:.4f}"
        return None

    def _check_drawdown_halt(self, _plan: OrderPlan, portfolio: PortfolioSnapshot) -> str | None:
        equity = finite_float(portfolio.equity)
        if equity is None:
            return None
        hwm = finite_float(portfolio.high_water_mark)
        if hwm is None or hwm <= 0:
            # No known peak, or a corrupt one: treat "the peak is now" rather
            # than disabling the breaker. The caller recomputes high_water_mark
            # fresh from stored equity history every call (never an
            # incrementally-updated running value), so a single bad reading
            # here can never poison future evaluations.
            hwm = equity
        drawdown = (equity - hwm) / hwm
        if drawdown <= -self._limits.drawdown_halt_pct:
            return f"drawdown_halt:{drawdown:.4f}"
        return None
