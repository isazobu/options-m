"""Tests for ExecutionAgent's helpers.

The position limits are written in structures — "at most one open trade per
underlying", "at most five open trades" — but Alpaca reports one position per
leg. Everything here guards the translation between the two, because getting
it wrong lets a single four-leg condor consume the whole portfolio budget.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from options_m.agents.execution import _group_into_structures


def _position(symbol: str) -> dict[str, Any]:
    return {"symbol": symbol, "asset_class": "us_option"}


def test_the_four_legs_of_one_condor_count_as_one_structure() -> None:
    legs = [
        _position("SPY250321P00095000"),
        _position("SPY250321P00090000"),
        _position("SPY250321C00105000"),
        _position("SPY250321C00110000"),
    ]

    assert _group_into_structures(legs) == {("SPY", date(2025, 3, 21))}


def test_two_expiries_on_one_underlying_are_two_structures() -> None:
    """Legs of one structure share an expiry; different expiries are separate trades."""
    legs = [
        _position("SPY250321P00095000"),
        _position("SPY250321P00090000"),
        _position("SPY250418P00095000"),
        _position("SPY250418P00090000"),
    ]

    assert _group_into_structures(legs) == {
        ("SPY", date(2025, 3, 21)),
        ("SPY", date(2025, 4, 18)),
    }


def test_different_underlyings_never_merge() -> None:
    legs = [_position("SPY250321C00105000"), _position("QQQ250321C00105000")]

    assert len(_group_into_structures(legs)) == 2


def test_an_unparseable_symbol_counts_as_its_own_structure() -> None:
    """The limits cap exposure, so an unrecognised position must never shrink
    the count — the safe direction is to over-count, never to under-count."""
    legs = [
        _position("SPY250321C00105000"),
        _position("not-an-occ-symbol"),
        _position(""),
    ]

    assert len(_group_into_structures(legs)) == 3


def test_no_positions_is_no_structures() -> None:
    assert _group_into_structures([]) == set()
