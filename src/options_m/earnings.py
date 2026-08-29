"""Earnings-date calendar for the fixed trading universe.

Neither Alpaca's Trading API nor its Market Data API exposes an earnings
calendar. ``get_corporate_actions`` / ``get_corporate_action_announcements``
cover dividends, mergers, splits and spinoffs only — confirmed by inspecting
both OpenAPI specs bundled with the official ``alpaca-mcp-server``. Selling
premium into an earnings print is the single most common way a short-vol
options strategy blows up (a name-specific IV crush or gap can blow through
a credit spread's short strike), so until Alpaca or a paid data vendor gives
us this natively, the dates below are maintained by hand.

Sourced 2026-08-29 from TipRanks, MarketChameleon and InvestingCalendar.
``confidence="confirmed"`` means the company has announced the date;
``confidence="estimated"`` means it is a third-party forecast based on the
company's historical reporting cadence and can shift by several days — a
mis-estimate on those names is exactly the failure mode this module exists
to prevent, so treat "estimated" as a reason to widen the blackout window,
not to skip it. ETFs (SPY, QQQ, IWM) carry no single-company earnings risk
and are intentionally absent from this dict.

This needs periodic refresh: dates here become stale, "estimated" entries
get confirmed or shift, and new fiscal quarters roll around. Re-check every
symbol before relying on this for a live run more than a few weeks old.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

Confidence = Literal["confirmed", "estimated"]

LAST_REFRESHED = date(2026, 8, 29)


@dataclass(frozen=True, slots=True)
class EarningsDate:
    """One company's next known/expected earnings date."""

    date: date
    confidence: Confidence
    note: str = ""


# Next earnings date per underlying, as of LAST_REFRESHED. For an
# "estimated" entry with an analyst-forecast range, the *earliest* day of
# the range is stored so a blackout window starts conservatively early.
EARNINGS: dict[str, EarningsDate] = {
    "AAPL": EarningsDate(date(2026, 10, 29), "estimated", note="fiscal Q4 2026"),
    "MSFT": EarningsDate(date(2026, 10, 27), "confirmed", note="fiscal Q1 FY2027"),
    "GOOGL": EarningsDate(date(2026, 10, 27), "confirmed", note="Q3 2026"),
    "META": EarningsDate(date(2026, 10, 28), "confirmed", note="Q3 2026"),
    "AMD": EarningsDate(
        date(2026, 10, 29), "estimated", note="Q3 2026; forecast range Oct 29 - Nov 5"
    ),
    "TSLA": EarningsDate(
        date(2026, 10, 21), "estimated", note="Q3 2026; forecast range Oct 21-23"
    ),
    "NVDA": EarningsDate(date(2026, 11, 25), "estimated", note="fiscal Q3 FY2027"),
}


def next_earnings(symbol: str) -> EarningsDate | None:
    """Return the next known/expected earnings date for `symbol`, or None.

    None means either the symbol has no single-company earnings risk (an
    ETF) or it simply is not tracked here yet — callers must not treat
    "no entry" as "no earnings risk" for an individual stock; treat an
    untracked non-ETF symbol as unsafe until this dict is extended.
    """
    return EARNINGS.get(symbol.upper())


def is_earnings_blackout(
    symbol: str, as_of: date, days_before: int = 3, days_after: int = 1
) -> bool:
    """True if `as_of` falls inside the earnings blackout window for `symbol`.

    Default window: 3 calendar days before the print through 1 day after,
    which comfortably covers both a pre-earnings IV run-up and the
    post-earnings gap. Widen `days_before` for an "estimated" entry if you
    want extra margin against the date shifting.
    """
    entry = next_earnings(symbol)
    if entry is None:
        return False
    delta_days = (entry.date - as_of).days
    return -days_after <= delta_days <= days_before
