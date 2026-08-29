"""Tests for contract selection and pricing.

This is the anti-hallucination boundary: the caller states a structure and a
target delta, and this module picks the real contracts. The cases that matter
most are the refusals — a structure it cannot build must be refused by name,
never bent into a different one.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from options_m import strategy_builder
from options_m.config import Settings
from options_m.models import OrderPlan, Rejection, StrategyIntent

_EXPIRY = date.today() + timedelta(days=30)
_SPOT = 100.0


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"database_url": None}
    base.update(overrides)
    return Settings(**base)


def _intent(**overrides: Any) -> StrategyIntent:
    base: dict[str, Any] = {
        "action": "open",
        "strategy": "long_call",
        "underlying": "SPY",
        "target_delta": 0.25,
        "dte_min": 21,
        "dte_max": 38,
        "conviction": 0.8,
        "thesis": "test",
        "invalidation": "test",
    }
    base.update(overrides)
    return StrategyIntent(**base)


def _occ(strike: float, option_type: str) -> str:
    letter = "C" if option_type == "call" else "P"
    return f"SPY{_EXPIRY:%y%m%d}{letter}{int(strike * 1000):08d}"


def _contract(strike: float, option_type: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": _occ(strike, option_type),
        "type": option_type,
        "status": "active",
        "tradable": True,
        "strike_price": str(strike),
        "expiration_date": _EXPIRY.isoformat(),
        "open_interest": "500",
    }
    base.update(overrides)
    return base


def _snapshot(
    *, bid: float, ask: float, delta: float | None = None, iv: float | None = None
) -> dict[str, Any]:
    snap: dict[str, Any] = {"latestQuote": {"bp": bid, "ap": ask}}
    if delta is not None:
        snap["greeks"] = {"delta": delta}
    if iv is not None:
        snap["impliedVolatility"] = iv
    return snap


def _chain(
    strikes: list[float], *, with_greeks: bool = True
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """A small two-sided chain around ``_SPOT`` with tight, liquid quotes."""
    contracts: list[dict[str, Any]] = []
    snapshots: dict[str, dict[str, Any]] = {}
    for strike in strikes:
        for option_type in ("call", "put"):
            contract = _contract(strike, option_type)
            contracts.append(contract)
            # Rough but monotonic: further OTM is cheaper and lower delta.
            moneyness = (strike - _SPOT) / _SPOT
            price = max(0.6, 5.0 - abs(moneyness) * 30.0)
            delta = 0.5 - moneyness * 4 if option_type == "call" else -0.5 - moneyness * 4
            snapshots[contract["symbol"]] = _snapshot(
                bid=price - 0.05,
                ask=price + 0.05,
                delta=delta if with_greeks else None,
                iv=0.25 if with_greeks else None,
            )
    return contracts, snapshots


_ACCOUNT = {"equity": "100000", "cash": "100000"}


async def _build(intent: StrategyIntent, **overrides: Any) -> OrderPlan | Rejection:
    contracts, snapshots = overrides.pop("chain", None) or _chain([90.0, 95.0, 100.0, 105.0, 110.0])
    kwargs: dict[str, Any] = {
        "contracts": contracts,
        "snapshots": snapshots,
        "account": _ACCOUNT,
        "existing_position": None,
        "settings": _settings(),
        "proposal_id": 1,
        "spot": _SPOT,
    }
    kwargs.update(overrides)
    return await strategy_builder.build(intent, **kwargs)


# ---------------------------------------------------------------------------
# Refusals — the safety-critical half
# ---------------------------------------------------------------------------

async def test_a_structure_with_no_builder_is_refused_by_name() -> None:
    """The matrix can emit these; building them as something else is the danger.

    Before this guard an unrecognised strategy was not in _CALL_STRATEGIES so
    it defaulted to puts, was not a vertical so it grew no second leg, and fell
    through _risk_profile into the cash-secured-put arm — an iron condor would
    have been submitted as a lone put.
    """
    for strategy in (
        "put_credit_spread",
        "call_credit_spread",
        "iron_condor",
        "iron_butterfly",
    ):
        result = await _build(_intent(strategy=strategy))
        assert isinstance(result, Rejection), f"{strategy} must not build"
        assert result.reason == "unsupported_strategy"
        assert result.detail["strategy"] == strategy


async def test_the_matrix_spelling_of_a_debit_vertical_builds() -> None:
    """matrix.py says call_debit_spread; this module grew up on debit_call_spread."""
    result = await _build(_intent(strategy="call_debit_spread", spread_width=5.0))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert result.strategy == "debit_call_spread"
    assert len(result.legs) == 2
    assert [leg.side for leg in result.legs] == ["buy", "sell"]


async def test_an_empty_dte_window_is_refused_not_widened() -> None:
    result = await _build(_intent(dte_min=200, dte_max=300))

    assert isinstance(result, Rejection)
    assert result.reason == "no_contracts_in_window"


# ---------------------------------------------------------------------------
# Implied-vol fallback
# ---------------------------------------------------------------------------

def test_iv_is_solved_from_the_mid_when_the_snapshot_carries_none() -> None:
    """Alpaca's paper feed returns greeks: None — without this nothing scores."""
    contracts, snapshots = _chain([100.0], with_greeks=False)

    without_spot = strategy_builder.normalize_contracts(contracts, snapshots)
    with_spot = strategy_builder.normalize_contracts(contracts, snapshots, spot=_SPOT)

    assert all(c.implied_volatility is None for c in without_spot)
    assert all(c.implied_volatility is not None and c.implied_volatility > 0 for c in with_spot)


async def test_a_plan_still_builds_when_the_chain_has_no_greeks() -> None:
    chain = _chain([90.0, 95.0, 100.0, 105.0, 110.0], with_greeks=False)

    result = await _build(_intent(strategy="long_call"), chain=chain)

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert result.legs[0].delta_source == "black_scholes"


# ---------------------------------------------------------------------------
# Long strangle
# ---------------------------------------------------------------------------

async def test_a_long_strangle_buys_a_call_and_a_put_on_one_expiry() -> None:
    result = await _build(_intent(strategy="long_strangle"))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert len(result.legs) == 2
    assert {leg.option_type for leg in result.legs} == {"call", "put"}
    assert all(leg.side == "buy" for leg in result.legs)
    assert len({leg.expiry for leg in result.legs}) == 1


async def test_a_strangle_keeps_the_call_strike_above_the_put_strike() -> None:
    """Equal strikes would be a straddle — a different structure and risk."""
    result = await _build(_intent(strategy="long_strangle"))

    assert isinstance(result, OrderPlan)
    call = next(leg for leg in result.legs if leg.option_type == "call")
    put = next(leg for leg in result.legs if leg.option_type == "put")
    assert call.strike > put.strike


async def test_a_strangles_max_loss_is_the_debit_and_its_profit_is_unbounded() -> None:
    result = await _build(_intent(strategy="long_strangle"))

    assert isinstance(result, OrderPlan)
    assert result.max_loss == result.limit_price * 100 * result.qty
    assert result.max_profit is None
    # Two breakevens cannot honestly be reported in one field.
    assert result.breakeven is None
