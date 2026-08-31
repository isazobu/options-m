"""Tests for the deterministic Strategy Matrix.

Scoped to the width handoff into StrategyIntent, which is what decides whether
the builder is allowed to size the wings from the expected move or has to use
the flat configured width.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from options_m import matrix
from options_m.config import Settings
from options_m.models import RegimeRead, StrategyIntent

_AS_OF = date(2026, 6, 15)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"database_url": None}
    base.update(overrides)
    return Settings(**base)


def _evidence(*, sma_20: float, sma_50: float, rsi_14: float, iv_rv: float) -> dict[str, Any]:
    return {
        "symbol": "SPY",
        "trend": {"sma_20": sma_20, "sma_50": sma_50, "rsi_14": rsi_14},
        "options": {"iv_atm": iv_rv * 0.20, "realised_vol_20d": 0.20},
        "options_trading_level": 3,
    }


def _flat_expensive(iv_rv: float) -> dict[str, Any]:
    return _evidence(sma_20=100.0, sma_50=100.0, rsi_14=50.0, iv_rv=iv_rv)


_REGIME = RegimeRead(thesis="t", invalidation="i", conviction=0.9)


def _decide(evidence: dict[str, Any], settings: Settings) -> StrategyIntent:
    decision = matrix.decide(evidence, _REGIME, settings=settings, as_of=_AS_OF)
    assert isinstance(decision, StrategyIntent), decision
    return decision


def test_a_flat_tape_with_expensive_premium_sells_an_iron_condor() -> None:
    assert _decide(_flat_expensive(1.20), _settings()).strategy == "iron_condor"


def test_very_expensive_premium_upgrades_the_condor_to_a_butterfly() -> None:
    assert _decide(_flat_expensive(1.50), _settings()).strategy == "iron_butterfly"


def test_the_width_is_left_unset_so_the_builder_can_size_the_wings() -> None:
    """The matrix has no chain, so it cannot know what a sensible width is.

    Leaving it unset is the signal that the builder — which holds the real
    contracts and their implied vols — should size the wings from the expected
    move instead of applying a flat dollar amount to every underlying.
    """
    intent = _decide(_flat_expensive(1.20), _settings())

    assert intent.spread_width is None


def test_the_flat_width_comes_back_when_scaling_is_switched_off() -> None:
    settings = _settings(
        spread_width_expected_move_mult=0.0,
        spread_width_expected_move_mult_atm=0.0,
        spread_width_default=5.0,
    )

    assert _decide(_flat_expensive(1.20), settings).spread_width == 5.0


def test_the_two_families_are_switched_off_independently() -> None:
    """An at-the-money structure reads its own multiplier, not the shared one."""
    settings = _settings(
        spread_width_expected_move_mult=0.0, spread_width_expected_move_mult_atm=1.25
    )

    # Condor takes the disabled multiplier and falls back to the flat width...
    assert _decide(_flat_expensive(1.20), settings).spread_width == 5.0
    # ...while the butterfly still gets to size its own wings.
    assert _decide(_flat_expensive(1.50), settings).spread_width is None


def test_a_single_leg_structure_never_carries_a_width() -> None:
    """A long strangle has no width concept — max loss is the premium paid."""
    cheap_flat = _evidence(sma_20=100.0, sma_50=100.0, rsi_14=50.0, iv_rv=0.80)

    intent = _decide(cheap_flat, _settings())

    assert intent.strategy == "long_strangle"
    assert intent.spread_width is None


# ---------------------------------------------------------------------------
# Bought premium — the "cheap" IV column, switchable off for a short campaign
# ---------------------------------------------------------------------------


def _up(iv_rv: float) -> dict[str, Any]:
    return _evidence(sma_20=105.0, sma_50=100.0, rsi_14=60.0, iv_rv=iv_rv)


def _down(iv_rv: float) -> dict[str, Any]:
    return _evidence(sma_20=95.0, sma_50=100.0, rsi_14=40.0, iv_rv=iv_rv)


def test_the_cheap_column_is_held_when_bought_premium_is_off() -> None:
    """Every cell that opens for a debit, across all three trend states."""
    for evidence, expected in (
        (_up(0.90), "call_debit_spread"),
        (_flat_expensive(0.90), "long_strangle"),
        (_down(0.90), "put_debit_spread"),
    ):
        allowed = _decide(evidence, _settings(allow_bought_premium=True))
        assert allowed.strategy == expected

        blocked = matrix.decide(
            evidence,
            _REGIME,
            settings=_settings(allow_bought_premium=False),
            as_of=_AS_OF,
        )
        assert blocked == "hold"


def test_sold_premium_is_untouched_by_the_switch() -> None:
    """The switch takes the debit column off the board and nothing else."""
    settings = _settings(allow_bought_premium=False)
    for evidence, expected in (
        (_up(1.20), "put_credit_spread"),
        (_flat_expensive(1.20), "iron_condor"),
        (_down(1.50), "call_credit_spread"),
        (_flat_expensive(1.50), "iron_butterfly"),
    ):
        assert _decide(evidence, settings).strategy == expected


def test_a_level_two_downgrade_cannot_smuggle_a_long_single_through() -> None:
    """call_debit_spread degrades to long_call at Level 2. That is still bought
    premium, so the switch has to catch it after the downgrade, not before."""
    decision = matrix.decide(
        _up(0.90),
        _REGIME,
        settings=_settings(allow_bought_premium=False, options_level=2),
        as_of=_AS_OF,
    )
    assert decision == "hold"
