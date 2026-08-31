"""Reconstruct a trading year of daily ATM implied vol from option bars.

IV Rank is only a vol regime if its window is a year of daily observations. The
live pulse writes one reading a minute, so on a cold database the rank has
nothing to rank against for months. This module fills that history in from data
Alpaca actually serves, so the number is real from the first pull rather than
``NO_DATA_AVAILABLE`` until next summer.

What is available, and what is not
----------------------------------
There is no historical option *quote* endpoint — ``/v1beta1/options/quotes``
exists only as ``/latest``, and the OPRA agreement this account has not signed
would not add one. What there is: ``/v1beta1/options/bars``, daily OHLCV per
contract, and it serves *expired* contracts, whose reference data comes back
from ``get_option_contracts`` under ``status="inactive"``.

So a past session's option price can only be that session's **last trade**.
For each past session this module takes the ATM call's daily bar close, the
underlying's close from the same session, and solves Black-Scholes backwards
for the vol that reproduces it. That is the reconstruction a retail desk
without a vendor IV index would do by hand; every input is a real print.

Three ways it is weaker than a vendor IV Rank, none of them hidden:

1. **Timing.** The bar close is the day's last print, the stock close is
   16:00. On a liquid ATM contract those are minutes apart; on a quiet one they
   are hours, and pairing an 11:00 option price with a 16:00 spot puts the
   error into the vol. ``_MIN_TRADES_PER_BAR`` is the guard — a session whose
   ATM contract barely traded is dropped, not approximated.
2. **Trade price, not mid.** An IV solved from a print sits wherever the trade
   crossed, bid or ask, while the live series solves from the chain's mid. The
   two halves of the window are therefore not perfectly homogeneous. This is
   self-healing: after a year of live pulses the reconstructed half has rolled
   out of the window entirely.
3. **Discrete strike, jittery tenor.** Nearest listed strike, not interpolated,
   and monthly expiries only — so the tenor walks between roughly 18 and 52
   days as expiries roll, against the live read's 21-38. Monthlies are the
   choice because they are the contracts with a year of continuous volume
   behind them; weeklies from twelve months ago are too thin to invert.

Deliberately *not* done: statistical outlier removal. A median-absolute-
deviation filter over the year would clip the vol spikes — and a spike is
exactly the window maximum that IV Rank is measured against. Clipping it would
flatter every subsequent rank. The guards here are all physical (did it trade,
is it near the money, does the price invert to a sane vol); nothing is dropped
for being surprising.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from options_m.config import Settings
from options_m.mcp_client import AlpacaMcp, finite_float
from options_m.store import Store
from options_m.volatility import CALL, implied_vol

logger = logging.getLogger(__name__)

_EXCHANGE_TZ = ZoneInfo("America/New_York")
# Reconstructed readings are dated to the session close, which is where the
# underlying's close comes from. A regular US equity-option session ends here.
_SESSION_CLOSE = time(hour=16, minute=0)

# Monthly expiries sit ~30-31 days apart, so for any given session the nearest
# usable one lands somewhere in this band. Wider than the live pack's
# dte_target window on purpose: narrowing it would leave sessions with no
# expiry at all, and a gap in the series costs more than the tenor drift.
_BACKFILL_DTE_MIN = 18
_BACKFILL_DTE_MAX = 52

# How far a strike may sit from the session's close and still be read as
# at-the-money. Past this the smile, not the ATM level, is what gets measured.
_MAX_MONEYNESS_DRIFT = 0.10

# A daily bar with fewer prints than this is dropped: its close is too likely
# to be an hours-old trade paired against a 16:00 spot. This is the main
# defence against the timing mismatch described in the module docstring.
_MIN_TRADES_PER_BAR = 20

# Vols outside this band are an inversion artefact (a print at intrinsic, a
# mis-stamped strike), not a market. Wide enough to keep a genuine crisis.
_IV_SANITY_MIN = 0.02
_IV_SANITY_MAX = 3.0

# Strike band to pull reference data for, as a fraction either side of the
# closes that map to an expiry. Enough to hold the drift in spot across the
# ~3 weeks a single expiry covers.
_STRIKE_BAND = 0.08

# Option bars are dated at the session, so a UTC-midnight boundary cannot split
# one. Days, not timestamps, keeps the request readable in the logs.
_DATE_FMT = "%Y-%m-%d"


@dataclass
class BackfillReport:
    """What one backfill pass over one symbol did. Logged, and returned so the
    agent can put the counts in its run detail."""

    symbol: str
    sessions_missing: int = 0
    sessions_written: int = 0
    contracts_examined: int = 0
    expiries_used: int = 0
    dropped: Counter[str] = field(default_factory=Counter)

    @property
    def complete(self) -> bool:
        return self.sessions_missing == self.sessions_written

    def as_detail(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "sessions_missing": self.sessions_missing,
            "sessions_written": self.sessions_written,
            "contracts_examined": self.contracts_examined,
            "expiries_used": self.expiries_used,
            "dropped": dict(self.dropped),
        }


async def backfill_daily_iv(
    settings: Settings,
    mcp: AlpacaMcp,
    store: Store,
    symbol: str,
    *,
    days: int | None = None,
    today: date | None = None,
) -> BackfillReport:
    """Fill in ``symbol``'s missing daily ATM-IV observations from option bars.

    Incremental by construction: sessions already in ``iv_history`` are never
    refetched, so the first pass reconstructs a year and every later pass costs
    a handful of calls for the sessions since. Only sessions strictly before
    ``today`` are touched — the current one belongs to the live writer.
    """
    symbol = symbol.upper()
    window = days or settings.iv_rank_window_days
    today = today or datetime.now(UTC).astimezone(_EXCHANGE_TZ).date()
    report = BackfillReport(symbol=symbol)

    closes = await _session_closes(mcp, symbol, window)
    if not closes:
        logger.warning("iv backfill: no daily bars for %s", symbol)
        return report

    covered = await store.iv_session_days(symbol, days=window)
    missing = sorted(day for day in closes if day < today and day not in covered)
    report.sessions_missing = len(missing)
    if not missing:
        return report

    # Group the missing sessions by the expiry each one will be priced against,
    # so reference data and bars are fetched once per expiry rather than once
    # per session.
    by_expiry: dict[date, list[date]] = {}
    for session_day in missing:
        expiry = _expiry_for(session_day)
        if expiry is None:
            report.dropped["no_expiry_in_dte_band"] += 1
            continue
        by_expiry.setdefault(expiry, []).append(session_day)
    report.expiries_used = len(by_expiry)

    readings: list[dict[str, Any]] = []
    for expiry, sessions in sorted(by_expiry.items()):
        try:
            written = await _readings_for_expiry(
                mcp, symbol, expiry, sessions, closes, settings, report, today
            )
        except Exception:
            # One unreachable expiry must not cost the other eleven months.
            logger.warning(
                "iv backfill: expiry %s failed for %s",
                expiry.isoformat(),
                symbol,
                exc_info=True,
            )
            report.dropped["expiry_fetch_failed"] += len(sessions)
            continue
        readings.extend(written)

    if readings:
        report.sessions_written = await store.append_iv_snapshots(symbol, readings)

    logger.info("iv backfill", extra=report.as_detail())
    return report


async def _readings_for_expiry(
    mcp: AlpacaMcp,
    symbol: str,
    expiry: date,
    sessions: list[date],
    closes: dict[date, float],
    settings: Settings,
    report: BackfillReport,
    today: date,
) -> list[dict[str, Any]]:
    """Reconstruct one ATM-IV reading per session, all priced off ``expiry``."""
    spots = [closes[day] for day in sessions]
    contracts = await mcp.get_option_contracts(
        symbol,
        option_type=CALL,
        # A range, not the exact third Friday. When that Friday is a market
        # holiday the monthly expiry moves to the Thursday before it and the
        # Friday lists no contracts at all — 19 June 2026 is Juneteenth, and
        # asking for it exactly cost a month of sessions. Equity weeklies also
        # expire on Fridays, so a Thursday-to-Friday window can only return the
        # monthly, whichever of the two days it settled on.
        expiration_gte=(expiry - timedelta(days=1)).isoformat(),
        expiration_lte=expiry.isoformat(),
        strike_gte=min(spots) * (1.0 - _STRIKE_BAND),
        strike_lte=max(spots) * (1.0 + _STRIKE_BAND),
        # An expiry in the past is only reachable as an expired contract, and
        # Alpaca returns active ones unless asked otherwise.
        status="inactive" if expiry < today else "active",
    )
    listed = _strikes_by_expiry(contracts)
    if not listed:
        report.dropped["no_contracts_listed"] += len(sessions)
        return []
    # Whichever of the two days the monthly actually settled on is the one
    # carrying the strikes; the other, if present at all, is a stray.
    expiry, strikes = max(listed.items(), key=lambda item: (len(item[1]), item[0]))
    report.contracts_examined += len(strikes)

    # Bars for every strike in the band, not just the one nearest each close.
    # The nearest strike does not necessarily print every session, and asking
    # only for it threw away a quarter of the year over contracts that simply
    # did not trade that day. The batch is one request either way, so the
    # per-session choice below is made among strikes that actually traded.
    bars = await _bars_by_session(mcp, list(strikes.values()), sessions)

    readings: list[dict[str, Any]] = []
    for day in sessions:
        traded = [(strike, occ) for strike, occ in strikes.items() if day in bars.get(occ, {})]
        if not traded:
            report.dropped["no_bar_that_session"] += 1
            continue
        strike, occ = min(traded, key=lambda pair: abs(pair[0] - closes[day]))
        bar = bars[occ][day]
        reading = _invert_bar(
            symbol=symbol,
            occ=occ,
            session_day=day,
            expiry=expiry,
            strike=strike,
            spot=closes[day],
            bar=bar,
            settings=settings,
            report=report,
        )
        if reading is not None:
            readings.append(reading)
    return readings


def _invert_bar(
    *,
    symbol: str,
    occ: str,
    session_day: date,
    expiry: date,
    strike: float,
    spot: float,
    bar: dict[str, Any],
    settings: Settings,
    report: BackfillReport,
) -> dict[str, Any] | None:
    """Solve one session's ATM IV from its option bar close, or drop it.

    The contract's own properties are checked before the bar's: whether this
    strike is at the money at all does not depend on what it traded for, and
    diagnosing "the chain handed back a 200 strike against a 100 spot" is more
    use than "the 200 strike was worth nothing", which is a consequence of it.
    """
    dte = (expiry - session_day).days
    if dte <= 0:
        report.dropped["non_positive_dte"] += 1
        return None

    if abs(strike / spot - 1.0) > _MAX_MONEYNESS_DRIFT:
        report.dropped["strike_too_far_from_spot"] += 1
        return None

    trades = bar.get("n")
    if not isinstance(trades, int) or trades < _MIN_TRADES_PER_BAR:
        report.dropped["too_few_trades"] += 1
        return None

    price = finite_float(bar.get("c"))
    if price is None or price <= 0.0:
        report.dropped["no_close_price"] += 1
        return None

    iv = implied_vol(
        S=spot,
        K=strike,
        t=dte / 365.0,
        r=settings.risk_free_rate,
        q=0.0,
        price=price,
        kind=CALL,
    )
    if iv is None:
        report.dropped["iv_did_not_solve"] += 1
        return None
    if not (_IV_SANITY_MIN <= iv <= _IV_SANITY_MAX):
        report.dropped["iv_outside_sanity_band"] += 1
        return None

    return {
        "ts": datetime.combine(session_day, _SESSION_CLOSE, tzinfo=_EXCHANGE_TZ).astimezone(UTC),
        "iv_atm": iv,
        "dte": dte,
        "spot": spot,
        # Provenance, so a judge replaying the pack can tell a reconstructed
        # observation from a live one and see exactly what it was solved from.
        "payload": {
            "iv_atm": iv,
            "source": "option_bars_backfill",
            "contract": occ,
            "strike": strike,
            "expiry": expiry.isoformat(),
            "option_close": price,
            "option_trades": trades,
            "underlying_close": spot,
            "priced_from": "last_trade_of_session",
        },
    }


async def _session_closes(mcp: AlpacaMcp, symbol: str, window: int) -> dict[date, float]:
    """``{session day: underlying close}`` over the last ``window`` sessions.

    Doubles as the trading calendar for the backfill: a day with no daily bar
    was not a session, so it is not a hole in the IV series either.
    """
    bars = await mcp.get_stock_bars(symbol, timeframe="1Day", limit=window)
    closes: dict[date, float] = {}
    for bar in bars:
        day = _bar_date(bar)
        close = finite_float(bar.get("c"))
        if day is not None and close is not None and close > 0.0:
            closes[day] = close
    return closes


async def _bars_by_session(
    mcp: AlpacaMcp, occ_symbols: list[str], sessions: list[date]
) -> dict[str, dict[date, dict[str, Any]]]:
    """Daily bars for these contracts, indexed ``{occ: {session day: bar}}``."""
    start = min(sessions)
    end = max(sessions)
    out: dict[str, dict[date, dict[str, Any]]] = {}
    for batch in _batched(occ_symbols, 100):
        bars = await mcp.get_option_bars(
            batch,
            start=start.strftime(_DATE_FMT),
            end=end.strftime(_DATE_FMT),
            timeframe="1Day",
        )
        for occ, rows in bars.items():
            indexed = out.setdefault(occ, {})
            for row in rows:
                day = _bar_date(row)
                if day is not None:
                    indexed[day] = row
    return out


def _strikes_by_expiry(contracts: list[dict[str, Any]]) -> dict[date, dict[float, str]]:
    """``{expiry: {strike: OCC symbol}}`` from a contract listing.

    Grouped by the expiry each contract *reports*, rather than the one that was
    asked for, so a holiday-shifted monthly is priced at its real tenor.
    """
    index: dict[date, dict[float, str]] = {}
    for contract in contracts:
        strike = finite_float(contract.get("strike_price"))
        occ = contract.get("symbol")
        expiry = _parse_date(contract.get("expiration_date"))
        if (
            strike is not None
            and strike > 0.0
            and isinstance(occ, str)
            and occ
            and expiry is not None
        ):
            index.setdefault(expiry, {})[strike] = occ
    return index


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _expiry_for(session_day: date) -> date | None:
    """The standard monthly expiry to price ``session_day`` against.

    The third Friday whose days-to-expiry falls inside the backfill band and
    sits closest to the middle of it — the same "nearest usable tenor" rule the
    live pack applies to the chain, restricted to monthlies.
    """
    band_mid = (_BACKFILL_DTE_MIN + _BACKFILL_DTE_MAX) / 2
    candidates = [
        expiry
        for expiry in _monthly_expiries_around(session_day)
        if _BACKFILL_DTE_MIN <= (expiry - session_day).days <= _BACKFILL_DTE_MAX
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda expiry: abs((expiry - session_day).days - band_mid))


def _monthly_expiries_around(session_day: date) -> list[date]:
    """Third Fridays of this month and the next three."""
    expiries: list[date] = []
    year, month = session_day.year, session_day.month
    for _ in range(4):
        expiries.append(_third_friday(year, month))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return expiries


def _third_friday(year: int, month: int) -> date:
    """Where a month's standard monthly option expiry normally falls.

    A *target*, not the answer: when the third Friday is a market holiday the
    monthly settles on the Thursday before it. Rather than model the holiday
    calendar here, _readings_for_expiry lists both days and takes whichever one
    the broker says the contracts actually expire on.
    """
    first = date(year, month, 1)
    # weekday(): Monday is 0, Friday is 4.
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(days=14)


def _bar_date(bar: dict[str, Any]) -> date | None:
    """The session a bar belongs to, from its RFC-3339 ``t``."""
    stamp = bar.get("t")
    if not isinstance(stamp, str) or len(stamp) < 10:
        return None
    try:
        return date.fromisoformat(stamp[:10])
    except ValueError:
        return None


def _batched(items: list[str], size: int) -> list[list[str]]:
    return [items[start : start + size] for start in range(0, len(items), size)]
