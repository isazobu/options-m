"""Parse an OCC option symbol into its parts.

The chain hands us symbols like ``SPY   240920C00450000``; the strategy builder
and the evidence pack both need the strike, expiry and right broken out of that
string. This module only ever *reads* a symbol — it never constructs one. A
constructed OCC symbol that does not correspond to a listed contract is exactly
the hallucination the rest of the system is built to prevent, so the inverse
operation deliberately does not exist here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

OptionType = Literal["call", "put"]

# root (1-6 chars) + YYMMDD (6) + C|P (1) + strike in thousandths (8)
_TAIL_LEN = 15
_STRIKE_DIVISOR = 1000.0


@dataclass(frozen=True)
class OccOption:
    """The fields encoded in an OCC-21 option symbol."""

    underlying: str
    expiry: date
    option_type: OptionType
    strike: float


def parse_occ_symbol(symbol: str) -> OccOption | None:
    """Break an OCC option symbol into ``(underlying, expiry, type, strike)``.

    Returns ``None`` for anything that is not a well-formed OCC symbol — an
    equity ticker, a malformed string, a symbol with a nonsense date. Callers
    treat ``None`` as "not an option", never as an error to paper over.
    """
    compact = symbol.strip().upper().replace(" ", "")
    if len(compact) <= _TAIL_LEN:
        return None

    underlying = compact[:-_TAIL_LEN]
    tail = compact[-_TAIL_LEN:]
    if not underlying.isalpha():
        return None

    yymmdd, right, strike_digits = tail[:6], tail[6], tail[7:]
    if right not in ("C", "P") or not yymmdd.isdigit() or not strike_digits.isdigit():
        return None

    try:
        expiry = date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError:
        return None

    return OccOption(
        underlying=underlying,
        expiry=expiry,
        option_type="call" if right == "C" else "put",
        strike=int(strike_digits) / _STRIKE_DIVISOR,
    )
