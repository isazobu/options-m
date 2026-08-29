"""Tests for the option-volatility math.

Pure functions, no I/O — so these are plain synchronous tests. The Black-Scholes
identities (put/call parity, delta parity, a finite-difference delta) pin the
formulas; the IV Rank cases pin the tastytrade definition.
"""

from __future__ import annotations

import math

from options_m.volatility import (
    CALL,
    PUT,
    bsm_greeks,
    bsm_price,
    bsm_vega,
    implied_vol,
    iv_percentile,
    iv_rank,
    no_arb_bounds,
)

R, Q = 0.045, 0.0


def test_put_call_parity() -> None:
    s, k, t, r, q, sigma = 100.0, 95.0, 0.5, 0.04, 0.01, 0.3
    call = bsm_price(s, k, t, r, q, sigma, CALL)
    put = bsm_price(s, k, t, r, q, sigma, PUT)
    expected = s * math.exp(-q * t) - k * math.exp(-r * t)
    assert abs((call - put) - expected) < 1e-9


def test_implied_vol_round_trip() -> None:
    s, k, t = 100.0, 105.0, 40 / 365
    for kind in (CALL, PUT):
        for sigma in (0.05, 0.15, 0.3, 0.6, 1.2, 2.5):
            price = bsm_price(s, k, t, R, Q, sigma, kind)
            recovered = implied_vol(price, s, k, t, R, Q, kind)
            assert recovered is not None
            assert abs(recovered - sigma) < 1e-4, (kind, sigma, recovered)


def test_implied_vol_returns_none_outside_the_no_arb_band() -> None:
    s, k, t = 100.0, 50.0, 30 / 365
    lower, _ = no_arb_bounds(s, k, t, R, Q, CALL)
    assert implied_vol(lower, s, k, t, R, Q, CALL) is None
    assert implied_vol(None, 100, 100, 0.1, 0.04, 0.0, CALL) is None
    assert implied_vol(-1.0, 100, 100, 0.1, 0.04, 0.0, CALL) is None
    assert implied_vol(200.0, 100, 100, 0.1, 0.04, 0.0, CALL) is None


def test_vega_is_positive_and_peaks_near_the_money() -> None:
    atm = bsm_vega(100.0, 100.0, 0.25, 0.04, 0.0, 0.3)
    wing = bsm_vega(100.0, 140.0, 0.25, 0.04, 0.0, 0.3)
    assert atm > 0 and wing >= 0 and atm > wing


def test_greeks_delta_parity_and_sign() -> None:
    s, k, t, sigma = 100.0, 100.0, 0.08, 0.3
    for q in (0.0, 0.02):
        call = bsm_greeks(s, k, t, R, q, sigma, CALL)
        put = bsm_greeks(s, k, t, R, q, sigma, PUT)
        # delta_call - delta_put == e^{-qt}
        assert abs((call.delta - put.delta) - math.exp(-q * t)) < 1e-9
        assert 0.0 < call.delta < 1.0
        assert -1.0 < put.delta < 0.0
        assert call.gamma > 0 and put.gamma > 0
        assert call.vega > 0
        assert call.theta < 0  # a long option bleeds time value


def test_greeks_delta_matches_a_finite_difference() -> None:
    s, k, t, sigma = 100.0, 105.0, 0.08, 0.3
    h = 1e-4
    fd = (bsm_price(s + h, k, t, R, Q, sigma, CALL) - bsm_price(s - h, k, t, R, Q, sigma, CALL)) / (
        2 * h
    )
    assert abs(fd - bsm_greeks(s, k, t, R, Q, sigma, CALL).delta) < 1e-6


def test_greeks_are_defined_at_expiry() -> None:
    itm = bsm_greeks(100.0, 90.0, 0.0, R, Q, 0.3, CALL)
    otm = bsm_greeks(100.0, 110.0, -1.0, R, Q, 0.3, CALL)
    assert itm.delta == 1.0 and itm.gamma == 0.0 and itm.vega == 0.0
    assert otm.delta == 0.0


def test_iv_rank_basic() -> None:
    assert iv_rank([0.1, 0.2, 0.3]) == 100.0
    assert iv_rank([0.3, 0.2, 0.1]) == 0.0
    assert abs((iv_rank([0.1, 0.5, 0.3]) or 0.0) - 50.0) < 1e-9
    assert iv_rank([0.2, 0.2, 0.2]) == 0.0  # flat window -> defined as 0
    assert iv_rank([0.2]) is None  # need >= 2 observations
    assert iv_rank([]) is None
    assert iv_rank([None, 0.1, None, 0.3]) == 100.0  # nulls dropped


def test_iv_rank_with_explicit_current() -> None:
    values = [0.1, 0.2, 0.3, 0.4]
    assert iv_rank(values, current=0.25) == (0.25 - 0.1) / (0.4 - 0.1) * 100


def test_iv_percentile_basic() -> None:
    # last value 0.4; strictly below: 0.1, 0.2, 0.3 -> 3/5
    assert iv_percentile([0.1, 0.2, 0.3, 0.5, 0.4]) == 60.0
    assert iv_percentile([]) is None
    assert iv_percentile([0.1, 0.2, 0.3, 0.4], current=0.25) == 2 / 4 * 100
