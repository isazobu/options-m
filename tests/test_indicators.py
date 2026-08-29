"""Tests for the price-series indicators.

The bar inputs here are deliberately trivial (a straight ramp, a flat line) so
the expected values are arithmetic a reader can check by hand. The contract that
matters most: not enough data returns ``None``, never a number.
"""

from __future__ import annotations

from options_m.indicators import (
    atr,
    distance_from_high_pct,
    distance_from_low_pct,
    realised_volatility,
    rsi,
    sma,
    window_extremes,
)

_RAMP = [float(x) for x in range(1, 61)]  # 1.0 .. 60.0
_RAMP_BARS = [{"h": c + 1.0, "l": c - 1.0, "c": c} for c in _RAMP]


def test_sma_is_the_mean_of_the_last_window() -> None:
    assert sma(_RAMP, 10) == sum(range(51, 61)) / 10


def test_sma_needs_a_full_window() -> None:
    assert sma([1.0, 2.0, 3.0], 10) is None


def test_rsi_is_100_when_every_move_is_up() -> None:
    assert rsi(_RAMP, 14) == 100.0


def test_rsi_of_a_flat_series_is_neutral() -> None:
    assert rsi([50.0] * 30, 14) == 50.0


def test_rsi_needs_period_plus_one_points() -> None:
    assert rsi([1.0] * 10, 14) is None


def test_atr_of_constant_range_bars_is_that_range() -> None:
    value = atr(_RAMP_BARS, 14)
    assert value is not None
    assert abs(value - 2.0) < 1e-9


def test_atr_skips_bars_missing_a_leg() -> None:
    bars = [{"h": 1.0, "l": 0.0}] + _RAMP_BARS  # first bar has no close
    assert atr(bars, 14) is not None


def test_realised_volatility_is_positive_and_annualised() -> None:
    value = realised_volatility(_RAMP, 20)
    assert value is not None and value > 0.0


def test_realised_volatility_needs_more_than_the_window() -> None:
    assert realised_volatility([1.0] * 10, 20) is None


def test_distance_from_high_is_zero_at_a_new_high() -> None:
    assert distance_from_high_pct(_RAMP) == 0.0


def test_distance_from_low_is_positive_above_the_low() -> None:
    assert distance_from_low_pct(_RAMP, current=59.0) > 0.0


def test_window_extremes_returns_min_and_max() -> None:
    assert window_extremes(_RAMP) == (1.0, 60.0)


def test_window_extremes_of_nothing_is_none() -> None:
    assert window_extremes([None, None]) == (None, None)
