"""Tests for portfolio-level exposure.

The property that matters is aggregation: five correlated positions must read as
one large exposure, not five small ones. Direction and sign are asserted rather
than exact magnitudes — a sign error in the short/long handling would let a
short-vol book read as long-vol and pass a cap it should breach, without
changing any magnitude.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from options_m.exposure import (
    Exposure,
    beta_for,
    book_exposure,
    bs_vega,
    market_from_evidence,
    plan_exposure,
)
from options_m.models import Leg, OrderPlan

_SPOT = 640.0
_IV = 0.18
_RATE = 0.045
_EXPIRY = date.today() + timedelta(days=10)


def _leg(**overrides: Any) -> Leg:
    base: dict[str, Any] = {
        "symbol": "SPY250321P00630000",
        "side": "sell",
        "strike": 630.0,
        "expiry": _EXPIRY,
        "option_type": "put",
        "delta": -0.25,
        "bid": 3.0,
        "ask": 3.1,
        "open_interest": 500,
    }
    base.update(overrides)
    return Leg(**base)


def _credit_spread(qty: int = 1, underlying: str = "SPY") -> OrderPlan:
    return OrderPlan(
        proposal_id=1,
        underlying=underlying,
        strategy="put_credit_spread",
        legs=[
            _leg(side="sell", strike=630.0, delta=-0.25),
            _leg(side="buy", strike=623.0, delta=-0.12, symbol="SPY250321P00623000"),
        ],
        qty=qty,
        limit_price=-1.4,
        max_loss=560.0 * qty,
        client_order_id="om-1",
    )


def _position(symbol: str, qty: str) -> dict[str, Any]:
    return {"symbol": symbol, "qty": qty, "asset_class": "us_option"}


# ---------------------------------------------------------------------------
# Vega
# ---------------------------------------------------------------------------


def test_vega_is_quoted_per_vol_point_not_per_unit_of_vol() -> None:
    """A 1-point move in a 18%-vol quote, not a move from 0.18 to 1.18.

    The raw Black-Scholes derivative is per unit of vol; getting this wrong
    overstates every vega by 100x and the cap would never bind.
    """
    vega = bs_vega(spot=_SPOT, strike=_SPOT, dte_days=30, iv=_IV, risk_free_rate=_RATE)

    assert vega is not None
    # An at-the-money 30-day SPY option is worth tens of dollars per vol point,
    # not thousands.
    assert 10.0 < vega < 100.0


def test_vega_shrinks_with_time_which_is_why_short_dte_is_a_gamma_trade() -> None:
    near = bs_vega(spot=_SPOT, strike=_SPOT, dte_days=7, iv=_IV, risk_free_rate=_RATE)
    far = bs_vega(spot=_SPOT, strike=_SPOT, dte_days=30, iv=_IV, risk_free_rate=_RATE)

    assert near is not None and far is not None
    assert near < far


def test_undefined_inputs_give_no_vega_rather_than_a_number() -> None:
    assert bs_vega(spot=_SPOT, strike=_SPOT, dte_days=0, iv=_IV, risk_free_rate=_RATE) is None
    assert bs_vega(spot=_SPOT, strike=_SPOT, dte_days=10, iv=None, risk_free_rate=_RATE) is None
    assert bs_vega(spot=_SPOT, strike=_SPOT, dte_days=10, iv=0.0, risk_free_rate=_RATE) is None


# ---------------------------------------------------------------------------
# Plan exposure
# ---------------------------------------------------------------------------


def test_a_put_credit_spread_is_long_delta_and_short_vega() -> None:
    """The signs are the whole point: sell a put, you are long the underlying."""
    exposure = plan_exposure(
        _credit_spread(), spot=_SPOT, iv=_IV, risk_free_rate=_RATE, today=date.today()
    )

    assert exposure.beta_weighted_delta is not None
    assert exposure.net_vega is not None
    assert exposure.beta_weighted_delta > 0
    assert exposure.net_vega < 0


def test_exposure_scales_with_the_contract_count() -> None:
    one = plan_exposure(_credit_spread(qty=1), spot=_SPOT, iv=_IV, risk_free_rate=_RATE)
    four = plan_exposure(_credit_spread(qty=4), spot=_SPOT, iv=_IV, risk_free_rate=_RATE)

    assert one.beta_weighted_delta is not None and four.beta_weighted_delta is not None
    assert four.beta_weighted_delta == one.beta_weighted_delta * 4


def test_a_high_beta_name_reports_more_exposure_than_the_index() -> None:
    """Same structure, same deltas — correlation is what the cap has to price."""
    index = plan_exposure(
        _credit_spread(underlying="SPY"), spot=_SPOT, iv=_IV, risk_free_rate=_RATE
    )
    single = plan_exposure(
        _credit_spread(underlying="TSLA"), spot=_SPOT, iv=_IV, risk_free_rate=_RATE
    )

    assert index.beta_weighted_delta is not None and single.beta_weighted_delta is not None
    assert single.beta_weighted_delta > index.beta_weighted_delta
    assert beta_for("TSLA") > beta_for("SPY")


def test_an_unlisted_symbol_is_assumed_more_volatile_not_less() -> None:
    """Overstating an unknown name's beta sizes the book down, which is safe."""
    assert beta_for("ZZZZ") > 1.0


def test_a_leg_with_no_delta_makes_the_whole_plan_unknown() -> None:
    """Skipping it would be indistinguishable from a hedged leg."""
    plan = _credit_spread().model_copy(
        update={"legs": [_leg(side="sell", delta=None), _leg(side="buy", delta=-0.12)]}
    )

    exposure = plan_exposure(plan, spot=_SPOT, iv=_IV, risk_free_rate=_RATE)

    assert exposure.beta_weighted_delta is None
    assert exposure.net_vega is None
    assert exposure.incomplete_legs == 1


def test_no_implied_vol_leaves_delta_unknown_too() -> None:
    """One unmeasurable Greek makes the aggregate unmeasurable, not partial.

    A plan reporting a delta but no vega would pass the vega cap on an unknown,
    which is the direction that approves trades it should not.
    """
    exposure = plan_exposure(_credit_spread(), spot=_SPOT, iv=None, risk_free_rate=_RATE)

    assert exposure.net_vega is None
    assert exposure.beta_weighted_delta is None


# ---------------------------------------------------------------------------
# Book exposure — the aggregation property
# ---------------------------------------------------------------------------


def test_five_correlated_short_puts_read_as_one_large_exposure() -> None:
    """The failure max_concurrent_positions cannot see.

    Five separate underlyings, each one slot, each individually small — and the
    book is five times as directional as any one of them.
    """
    market = {sym: (_SPOT, _IV) for sym in ("SPY", "QQQ", "IWM", "NVDA", "META")}
    yymmdd = _EXPIRY.strftime("%y%m%d")
    one = book_exposure(
        [_position(f"SPY{yymmdd}P00630000", "-1")],
        market_by_symbol=market,
        risk_free_rate=_RATE,
    )
    five = book_exposure(
        [_position(f"{sym}{yymmdd}P00630000", "-1") for sym in market],
        market_by_symbol=market,
        risk_free_rate=_RATE,
    )

    assert one.beta_weighted_delta is not None and five.beta_weighted_delta is not None
    assert five.beta_weighted_delta > 4 * one.beta_weighted_delta


def test_a_short_position_is_the_opposite_sign_of_a_long_one() -> None:
    yymmdd = _EXPIRY.strftime("%y%m%d")
    long_put = book_exposure(
        [_position(f"SPY{yymmdd}P00630000", "1")],
        market_by_symbol={"SPY": (_SPOT, _IV)},
        risk_free_rate=_RATE,
    )
    short_put = book_exposure(
        [_position(f"SPY{yymmdd}P00630000", "-1")],
        market_by_symbol={"SPY": (_SPOT, _IV)},
        risk_free_rate=_RATE,
    )

    assert long_put.beta_weighted_delta is not None
    assert short_put.beta_weighted_delta is not None
    assert long_put.beta_weighted_delta < 0 < short_put.beta_weighted_delta
    assert long_put.net_vega is not None and short_put.net_vega is not None
    assert short_put.net_vega < 0 < long_put.net_vega


def test_an_empty_book_is_zero_not_unknown() -> None:
    """Nothing open is a measured fact; it must not block the first trade."""
    exposure = book_exposure([], market_by_symbol={}, risk_free_rate=_RATE)

    assert exposure.beta_weighted_delta == 0.0
    assert exposure.net_vega == 0.0


def test_stock_legs_are_not_counted_as_options() -> None:
    exposure = book_exposure(
        [{"symbol": "SPY", "qty": "100", "asset_class": "us_equity"}],
        market_by_symbol={"SPY": (_SPOT, _IV)},
        risk_free_rate=_RATE,
    )

    assert exposure.beta_weighted_delta == 0.0


def test_a_symbol_the_cache_has_never_seen_makes_the_book_unmeasurable() -> None:
    """An unmeasurable book must not read as an empty one."""
    yymmdd = _EXPIRY.strftime("%y%m%d")
    exposure = book_exposure(
        [_position(f"SPY{yymmdd}P00630000", "-1")],
        market_by_symbol={},
        risk_free_rate=_RATE,
    )

    assert exposure.beta_weighted_delta is None
    assert exposure.incomplete_legs == 1


# ---------------------------------------------------------------------------
# Combining, and reading the evidence cache
# ---------------------------------------------------------------------------


def test_combining_with_an_unknown_side_stays_unknown() -> None:
    known = Exposure(beta_weighted_delta=1_000.0, net_vega=-50.0)

    assert known.combined_with(Exposure.unknown(1)).beta_weighted_delta is None
    assert known.combined_with(known).beta_weighted_delta == 2_000.0


def test_the_spot_preference_matches_the_evidence_collector() -> None:
    """last -> day_close -> mid, or the greeks price a different underlying."""
    assert market_from_evidence(
        {"spot": {"last": 640.0, "day_close": 630.0, "mid": 620.0}, "options": {"iv_atm": 0.18}}
    ) == (640.0, 0.18)
    assert market_from_evidence(
        {"spot": {"last": "MISSING", "day_close": 630.0, "mid": 620.0}}
    ) == (630.0, None)
    assert market_from_evidence({"spot": {"last": "MISSING", "mid": 620.0}}) == (620.0, None)


def test_a_pack_with_no_usable_spot_is_no_market_data() -> None:
    assert market_from_evidence({"spot": {"last": "MISSING"}}) is None
    assert market_from_evidence({"spot": "MISSING"}) is None
    assert market_from_evidence({}) is None
