"""Tests for the OCC option-symbol parser.

The parser only ever reads a symbol; there is no inverse. These assertions pin
the read, and — just as important — that a non-option string comes back as
``None`` rather than a plausible-looking wrong answer.
"""

from __future__ import annotations

from datetime import date

import pytest

from options_m.occ import parse_occ_symbol


def test_parses_a_standard_call() -> None:
    parsed = parse_occ_symbol("SPY240920C00450000")

    assert parsed is not None
    assert parsed.underlying == "SPY"
    assert parsed.expiry == date(2024, 9, 20)
    assert parsed.option_type == "call"
    assert parsed.strike == 450.0


def test_parses_a_fractional_strike_put_with_padding() -> None:
    parsed = parse_occ_symbol("AAPL  251219P00190500")

    assert parsed is not None
    assert parsed.option_type == "put"
    assert parsed.strike == 190.5
    assert parsed.underlying == "AAPL"


@pytest.mark.parametrize(
    "symbol",
    [
        "AAPL",  # a plain equity ticker
        "",
        "SPY240920X00450000",  # not a call or a put
        "SPY241320C00450000",  # month 13
        "SPY2409ZZC00450000",  # non-numeric date
        "240920C00450000",  # no underlying
        "SP_240920C00450000",  # non-alpha underlying
    ],
)
def test_non_options_return_none(symbol: str) -> None:
    assert parse_occ_symbol(symbol) is None
