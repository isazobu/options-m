"""Tests for the risk engine.

Every rule is exercised in isolation: a plan/portfolio pair that trips
exactly one rule, keeping everything else clean, so a failure here always
names the actual rule that broke rather than "something in risk.py".
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from options_m.models import Leg, OrderPlan
from options_m.risk import PortfolioSnapshot, RiskEngine, RiskLimits

_LIMITS = RiskLimits(
    max_premium_pct_per_trade=0.02,
    max_total_premium_pct=0.15,
    max_concurrent_positions=5,
    max_positions_per_underlying=1,
    dte_min=7,
    dte_max=45,
    min_open_interest=100,
    max_spread_pct=0.10,
    daily_loss_halt_pct=0.03,
    drawdown_halt_pct=0.08,
    minutes_before_close_blackout=15,
)


def _leg(**overrides: Any) -> Leg:
    base: dict[str, Any] = {
        "symbol": "SPY250321C00100000",
        "side": "buy",
        "strike": 100.0,
        "expiry": date.today() + timedelta(days=21),
        "option_type": "call",
        "bid": 1.9,
        "ask": 2.1,
        "open_interest": 500,
    }
    base.update(overrides)
    return Leg(**base)


def _plan(**overrides: Any) -> OrderPlan:
    base: dict[str, Any] = {
        "proposal_id": 1,
        "underlying": "SPY",
        "strategy": "long_call",
        "legs": [_leg()],
        "qty": 1,
        "limit_price": 2.0,
        "max_loss": 200.0,
        "client_order_id": "om-1",
    }
    base.update(overrides)
    return OrderPlan(**base)


def _portfolio(**overrides: Any) -> PortfolioSnapshot:
    base: dict[str, Any] = {
        "equity": 100_000.0,
        "start_of_day_equity": 100_000.0,
        "high_water_mark": 100_000.0,
        "concurrent_option_positions": 0,
        "positions_in_underlying": 0,
        "total_open_option_premium": 0.0,
        "market_is_open": True,
        "minutes_to_close": 120.0,
        "kill_switch_engaged": False,
        "already_submitted": False,
    }
    base.update(overrides)
    return PortfolioSnapshot(**base)


def _engine() -> RiskEngine:
    return RiskEngine(_LIMITS)


def test_a_clean_plan_is_approved() -> None:
    verdict = _engine().evaluate(_plan(), _portfolio())

    assert verdict.approved is True
    assert verdict.reasons == []


def test_kill_switch_blocks_an_otherwise_clean_plan() -> None:
    verdict = _engine().evaluate(_plan(), _portfolio(kill_switch_engaged=True))

    assert verdict.approved is False
    assert verdict.reasons == ["kill_switch_engaged"]


def test_idempotent_duplicate_is_rejected() -> None:
    verdict = _engine().evaluate(_plan(), _portfolio(already_submitted=True))

    assert "idempotent_duplicate" in verdict.reasons


def test_market_closed_is_rejected() -> None:
    verdict = _engine().evaluate(_plan(), _portfolio(market_is_open=False))

    assert "market_closed" in verdict.reasons


def test_the_close_blackout_window_is_enforced() -> None:
    verdict = _engine().evaluate(_plan(), _portfolio(minutes_to_close=5.0))

    assert "close_blackout" in verdict.reasons


def test_a_naked_short_leg_is_always_rejected() -> None:
    plan = _plan(strategy="long_call", legs=[_leg(side="sell")])

    verdict = _engine().evaluate(plan, _portfolio())

    assert "naked_short_leg" in verdict.reasons


def test_a_debit_spread_with_a_covering_long_leg_is_not_naked() -> None:
    plan = _plan(
        strategy="debit_call_spread",
        legs=[_leg(side="buy", strike=100.0), _leg(side="sell", strike=105.0)],
    )

    verdict = _engine().evaluate(plan, _portfolio())

    assert "naked_short_leg" not in verdict.reasons


def test_a_debit_spread_missing_its_long_leg_is_naked() -> None:
    plan = _plan(strategy="debit_call_spread", legs=[_leg(side="sell", strike=105.0)])

    verdict = _engine().evaluate(plan, _portfolio())

    assert "naked_short_leg" in verdict.reasons


def test_dte_outside_the_window_is_rejected() -> None:
    plan = _plan(legs=[_leg(expiry=date.today() + timedelta(days=2))])

    verdict = _engine().evaluate(plan, _portfolio())

    assert any(r.startswith("dte_out_of_window") for r in verdict.reasons)


def test_insufficient_open_interest_is_rejected() -> None:
    plan = _plan(legs=[_leg(open_interest=1)])

    verdict = _engine().evaluate(plan, _portfolio())

    assert any(r.startswith("insufficient_open_interest") for r in verdict.reasons)


def test_a_wide_spread_is_rejected() -> None:
    plan = _plan(legs=[_leg(bid=1.0, ask=3.0)])

    verdict = _engine().evaluate(plan, _portfolio())

    assert any(r.startswith("wide_spread") for r in verdict.reasons)


def test_premium_per_trade_over_the_limit_is_rejected() -> None:
    plan = _plan(max_loss=100_000.0)

    verdict = _engine().evaluate(plan, _portfolio())

    assert "premium_per_trade_exceeded" in verdict.reasons


def test_unknown_equity_blocks_the_premium_checks() -> None:
    verdict = _engine().evaluate(_plan(), _portfolio(equity=None))

    assert "unknown_equity" in verdict.reasons


def test_total_open_premium_over_the_limit_is_rejected() -> None:
    plan = _plan(max_loss=1_000.0)

    verdict = _engine().evaluate(plan, _portfolio(total_open_option_premium=20_000.0))

    assert "total_premium_exceeded" in verdict.reasons


def test_max_concurrent_positions_is_enforced() -> None:
    verdict = _engine().evaluate(_plan(), _portfolio(concurrent_option_positions=5))

    assert "max_concurrent_positions" in verdict.reasons


def test_max_positions_per_underlying_is_enforced() -> None:
    verdict = _engine().evaluate(_plan(), _portfolio(positions_in_underlying=1))

    assert "max_positions_per_underlying" in verdict.reasons


def test_the_daily_loss_halt_trips_on_a_large_drop() -> None:
    verdict = _engine().evaluate(
        _plan(), _portfolio(equity=90_000.0, start_of_day_equity=100_000.0)
    )

    assert any(r.startswith("daily_loss_halt") for r in verdict.reasons)


def test_the_drawdown_halt_trips_below_the_high_water_mark() -> None:
    verdict = _engine().evaluate(
        _plan(), _portfolio(equity=90_000.0, high_water_mark=100_000.0)
    )

    assert any(r.startswith("drawdown_halt") for r in verdict.reasons)


def test_a_nan_high_water_mark_does_not_permanently_disable_the_drawdown_breaker() -> None:
    """The caller recomputes high_water_mark fresh every call, so one bad
    reading here must never look like "the breaker is broken forever"."""
    engine = _engine()
    poisoned = _portfolio(equity=90_000.0, high_water_mark=float("nan"))

    verdict = engine.evaluate(_plan(), poisoned)
    assert not any(r.startswith("drawdown_halt") for r in verdict.reasons)

    healthy = _portfolio(equity=90_000.0, high_water_mark=100_000.0)
    verdict = engine.evaluate(_plan(), healthy)
    assert any(r.startswith("drawdown_halt") for r in verdict.reasons)
