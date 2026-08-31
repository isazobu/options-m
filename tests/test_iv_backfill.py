"""Tests for the daily ATM-IV reconstruction.

The fake broker prices its option bars with the *forward* Black-Scholes model
the backfill inverts, at a vol it chooses per session. So every assertion about
a recovered IV is a real round trip — price a contract at 28% vol, hand back the
bar, and check the reconstruction says 28% — rather than a fixture agreeing with
itself.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from options_m.config import Settings
from options_m.db import Database
from options_m.iv_backfill import BackfillReport, backfill_daily_iv
from options_m.store import Store
from options_m.volatility import CALL, bsm_price

_EXCHANGE_TZ = ZoneInfo("America/New_York")
# A Monday, chosen so the sessions below are all weekdays.
_FIRST_SESSION = date(2026, 1, 5)
# _third_friday(2026, 2) — what a session in early January maps to at ~30 DTE.
_FEB_EXPIRY = date(2026, 2, 20)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"database_url": None, "risk_free_rate": 0.045}
    base.update(overrides)
    return Settings(**base)


def _store() -> Store:
    return Store(Database(Settings(database_url=None)))


async def _run(
    mcp: Any, store: Store, *, symbol: str = "SPY", today: date | None = None
) -> BackfillReport:
    """One backfill pass over the fake broker.

    ``today`` defaults to the day after the fake's last session, so every
    session it serves is a past one and therefore in scope. ``mcp`` is
    deliberately ``Any``: the fake implements the three reads the backfill
    makes, not the whole AlpacaMcp surface.
    """
    if today is None:
        last = mcp.sessions[-1] if mcp.sessions else _FIRST_SESSION
        today = last + timedelta(days=1)
    return await backfill_daily_iv(_settings(), mcp, store, symbol, today=today)


def _sessions(count: int, start: date = _FIRST_SESSION) -> list[date]:
    """``count`` consecutive weekdays from ``start``."""
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _session_close(day: date) -> datetime:
    return datetime.combine(day, time(hour=16), tzinfo=_EXCHANGE_TZ).astimezone(UTC)


class _FakeMcp:
    """The three reads the backfill makes, priced by the real BSM model.

    ``vols`` sets the true vol per session; anything unlisted uses
    ``base_vol``. ``trades`` sets the bar's print count, which is the quality
    gate the backfill applies before trusting a close.
    """

    def __init__(
        self,
        *,
        sessions: list[date] | None = None,
        spot: float = 100.0,
        strikes: tuple[float, ...] = (95.0, 100.0, 105.0),
        vols: dict[date, float] | None = None,
        base_vol: float = 0.28,
        trades: int = 500,
        spots: dict[date, float] | None = None,
        contracts_raise: bool = False,
        no_contracts: bool = False,
        ignore_strike_band: bool = False,
        holiday_expiries: frozenset[date] = frozenset(),
    ) -> None:
        self.sessions = sessions if sessions is not None else _sessions(140)
        self.spot = spot
        self.strikes = strikes
        self.vols = vols or {}
        self.base_vol = base_vol
        self.trades = trades
        self.spots = spots or {}
        self.contracts_raise = contracts_raise
        self.no_contracts = no_contracts
        self.ignore_strike_band = ignore_strike_band
        self.holiday_expiries = holiday_expiries
        self.contract_status: list[str] = []
        self.bar_batches: list[list[str]] = []

    # -- reads -----------------------------------------------------------
    async def get_stock_bars(
        self, symbol: str, *, timeframe: str = "1Day", limit: int = 252
    ) -> list[dict[str, Any]]:
        return [
            {"t": f"{day.isoformat()}T05:00:00Z", "c": self._spot_on(day), "v": 1_000_000}
            for day in self.sessions[-limit:]
        ]

    async def get_option_contracts(
        self,
        underlying: str,
        *,
        option_type: str | None = None,
        expiration_gte: str | None = None,
        expiration_lte: str | None = None,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        status: str = "active",
        limit: int = 1000,
        max_pages: int = 25,
    ) -> list[dict[str, Any]]:
        if self.contracts_raise:
            msg = "contracts unavailable"
            raise RuntimeError(msg)
        self.contract_status.append(status)
        if self.no_contracts:
            return []
        # The real listing is asked for a Thursday-to-Friday range and answers
        # with whichever day the monthly actually settled on.
        expiry = self.settled_expiry(date.fromisoformat(str(expiration_lte)))
        if expiry is None:
            return []
        return [
            {
                "symbol": _occ(underlying, expiry, strike),
                "strike_price": str(strike),
                "expiration_date": expiry.isoformat(),
            }
            for strike in self.strikes
            if self.ignore_strike_band
            or (
                (strike_gte is None or strike >= strike_gte)
                and (strike_lte is None or strike <= strike_lte)
            )
        ]

    def settled_expiry(self, third_friday: date) -> date | None:
        """Where the monthly actually landed. ``holiday_expiries`` marks a third
        Friday the market was shut on, which moves the monthly a day earlier."""
        if third_friday in self.holiday_expiries:
            return third_friday - timedelta(days=1)
        return third_friday

    async def get_option_bars(
        self,
        occ_symbols: list[str],
        *,
        start: str,
        end: str,
        timeframe: str = "1Day",
        limit: int = 10_000,
        max_pages: int = 25,
    ) -> dict[str, list[dict[str, Any]]]:
        self.bar_batches.append(list(occ_symbols))
        window = [
            day
            for day in self.sessions
            if date.fromisoformat(start) <= day <= date.fromisoformat(end)
        ]
        out: dict[str, list[dict[str, Any]]] = {}
        for occ in occ_symbols:
            expiry, strike = _parse_occ(occ)
            rows = []
            for day in window:
                dte = (expiry - day).days
                if dte <= 0:
                    continue
                spot = self._spot_on(day)
                price = bsm_price(
                    spot, strike, dte / 365.0, 0.045, 0.0, self._vol_on(day), CALL
                )
                rows.append(
                    {
                        "t": f"{day.isoformat()}T05:00:00Z",
                        "o": round(price, 4),
                        "c": round(price, 4),
                        "v": 1_000,
                        "n": self.trades,
                    }
                )
            if rows:
                out[occ] = rows
        return out

    # -- helpers ---------------------------------------------------------
    def _vol_on(self, day: date) -> float:
        return self.vols.get(day, self.base_vol)

    def _spot_on(self, day: date) -> float:
        return self.spots.get(day, self.spot)


def _occ(underlying: str, expiry: date, strike: float) -> str:
    return f"{underlying}{expiry:%y%m%d}C{round(strike * 1000):08d}"


def _parse_occ(occ: str) -> tuple[date, float]:
    body = occ[-15:]
    expiry = datetime.strptime(body[:6], "%y%m%d").date()
    return expiry, int(body[7:]) / 1000.0


# ---------------------------------------------------------------------------


async def test_a_year_of_sessions_is_reconstructed_from_option_bars() -> None:
    mcp = _FakeMcp()
    store = _store()

    report = await _run(mcp, store, symbol="spy")

    assert report.sessions_missing == len(mcp.sessions)
    assert report.sessions_written == report.sessions_missing
    assert report.complete
    # One observation per session, which is the whole point.
    assert len(await store.daily_iv_history("SPY", days=252)) == len(mcp.sessions)


async def test_the_recovered_vol_is_the_vol_the_bar_was_priced_at() -> None:
    """The round trip: forward-price at 33%, invert the bar, get 33% back."""
    sessions = _sessions(10)
    mcp = _FakeMcp(sessions=sessions, base_vol=0.33)
    store = _store()

    await _run(mcp, store)

    rows = await store.daily_iv_history("SPY")
    assert rows
    for row in rows:
        assert row["iv_atm"] == pytest.approx(0.33, abs=1e-4)


async def test_each_reading_is_dated_to_its_own_session_close() -> None:
    """Not to now — otherwise a year of history collapses onto today and the
    window is one session deep again."""
    sessions = _sessions(5)
    mcp = _FakeMcp(sessions=sessions)
    store = _store()

    await _run(mcp, store)

    assert await store.iv_session_days("SPY") == set(sessions)
    rows = await store.daily_iv_history("SPY")
    assert rows[0]["ts"] == _session_close(sessions[-1])


async def test_every_reading_carries_its_provenance() -> None:
    sessions = _sessions(3)
    mcp = _FakeMcp(sessions=sessions)
    store = _store()

    await _run(mcp, store)

    payload = (await store.daily_iv_history("SPY"))[0]["payload"]
    assert payload["source"] == "option_bars_backfill"
    assert payload["priced_from"] == "last_trade_of_session"
    assert payload["expiry"] == _FEB_EXPIRY.isoformat()
    assert payload["strike"] == 100.0
    assert payload["option_trades"] == 500


async def test_the_current_session_is_left_to_the_live_writer() -> None:
    sessions = _sessions(6)
    mcp = _FakeMcp(sessions=sessions)
    store = _store()

    await _run(mcp, store, today=sessions[-1])

    covered = await store.iv_session_days("SPY")
    assert sessions[-1] not in covered
    assert covered == set(sessions[:-1])


async def test_sessions_already_held_are_not_refetched() -> None:
    """The pass is incremental: a restart costs the days since, not the year."""
    sessions = _sessions(8)
    store = _store()
    for day in sessions[:-3]:
        await store.append_iv_snapshot("SPY", iv_atm=0.25, ts=_session_close(day))

    mcp = _FakeMcp(sessions=sessions)
    report = await _run(mcp, store)

    assert report.sessions_missing == 3
    assert report.sessions_written == 3
    # The pre-existing readings are untouched, not overwritten.
    rows = {row["session_day"]: row["iv_atm"] for row in await store.daily_iv_history("SPY")}
    assert rows[sessions[0]] == 0.25


async def test_a_second_pass_has_nothing_left_to_do() -> None:
    sessions = _sessions(6)
    mcp = _FakeMcp(sessions=sessions)
    store = _store()
    today = sessions[-1] + timedelta(days=1)

    await _run(mcp, store, today=today)
    calls_after_first = len(mcp.bar_batches)
    second = await _run(mcp, store, today=today)

    assert second.sessions_missing == 0
    assert len(mcp.bar_batches) == calls_after_first  # no bars refetched


async def test_a_thinly_traded_bar_is_dropped_rather_than_inverted() -> None:
    """The timing guard. A close that is one old print cannot be paired with a
    16:00 spot, so the session is a hole in the series, not a guess."""
    sessions = _sessions(5)
    mcp = _FakeMcp(sessions=sessions, trades=3)
    store = _store()

    report = await _run(mcp, store)

    assert report.sessions_written == 0
    assert report.dropped["too_few_trades"] == len(sessions)
    assert await store.daily_iv_history("SPY") == []


async def test_no_strike_inside_the_requested_band_writes_nothing() -> None:
    """A 100 spot asks for strikes within 8% of it; a chain that lists only a
    200 strike answers with nothing at all."""
    sessions = _sessions(4)
    mcp = _FakeMcp(sessions=sessions, strikes=(200.0,))
    store = _store()

    report = await _run(mcp, store)

    assert report.sessions_written == 0
    assert report.dropped["no_contracts_listed"] == len(sessions)


async def test_a_strike_too_far_from_spot_is_not_read_as_at_the_money() -> None:
    """The guard that does not trust the server's strike filter. Handed a 200
    strike against a 100 spot, the backfill drops the session rather than
    calling a deep-OTM vol the ATM level."""
    sessions = _sessions(4)
    mcp = _FakeMcp(sessions=sessions, strikes=(200.0,), ignore_strike_band=True)
    store = _store()

    report = await _run(mcp, store)

    assert report.sessions_written == 0
    assert report.dropped["strike_too_far_from_spot"] == len(sessions)


async def test_the_strike_follows_spot_across_the_expiry() -> None:
    """Spot drifts from 95 to 105 inside one expiry; each session is priced off
    the strike nearest *that* session's close, not the first one's."""
    sessions = _sessions(3)
    mcp = _FakeMcp(
        sessions=sessions,
        spots={sessions[0]: 95.0, sessions[1]: 100.0, sessions[2]: 105.0},
    )
    store = _store()

    await _run(mcp, store)

    strikes = {
        row["session_day"]: row["payload"]["strike"]
        for row in await store.daily_iv_history("SPY")
    }
    assert strikes == {sessions[0]: 95.0, sessions[1]: 100.0, sessions[2]: 105.0}


async def test_an_expired_expiry_is_requested_as_an_inactive_contract() -> None:
    sessions = _sessions(3)
    mcp = _FakeMcp(sessions=sessions)
    store = _store()

    await _run(mcp, store)

    # January sessions price off the February expiry, which is still ahead of
    # this "today" — so the listing is asked for as active.
    assert mcp.contract_status == ["active"]

    later = _sessions(3)
    mcp_past = _FakeMcp(sessions=later)
    await _run(mcp_past, _store(), today=_FEB_EXPIRY + timedelta(days=1))
    assert mcp_past.contract_status == ["inactive"]


async def test_a_holiday_shifted_monthly_is_still_found() -> None:
    """19 June 2026 is a third Friday and Juneteenth: the market is shut, the
    monthly settles on the Thursday, and asking for the Friday exactly listed
    nothing — which cost a whole month of sessions before the listing was
    widened to the Thursday as well."""
    sessions = _sessions(6)
    mcp = _FakeMcp(sessions=sessions, holiday_expiries=frozenset({_FEB_EXPIRY}))
    store = _store()

    report = await _run(mcp, store)

    assert report.sessions_written == len(sessions)
    assert report.dropped == {}
    rows = await store.daily_iv_history("SPY")
    # Priced at the real expiry, a day earlier than the third Friday, and the
    # DTE recorded is the real one rather than the target.
    shifted = _FEB_EXPIRY - timedelta(days=1)
    assert {row["payload"]["expiry"] for row in rows} == {shifted.isoformat()}
    assert all(row["dte"] == (shifted - row["session_day"]).days for row in rows)


async def test_a_vol_spike_survives_into_the_window() -> None:
    """Nothing statistical clips an outlier: the spike is the window maximum
    IV Rank is measured against, so removing it would flatter every rank."""
    sessions = _sessions(130)
    spike_day = sessions[60]
    mcp = _FakeMcp(sessions=sessions, base_vol=0.20, vols={spike_day: 0.85})
    store = _store()

    await _run(mcp, store)

    ivs = {row["session_day"]: row["iv_atm"] for row in await store.daily_iv_history("SPY")}
    assert ivs[spike_day] == pytest.approx(0.85, abs=1e-3)
    # And the rank reflects it: today's 20% sits at the bottom of a window
    # whose top is the spike.
    rank = await store.iv_rank_for("SPY", min_days=100)
    assert rank is not None
    assert rank < 5.0


async def test_one_unreachable_expiry_does_not_cost_the_others() -> None:
    sessions = _sessions(3)
    mcp = _FakeMcp(sessions=sessions, contracts_raise=True)
    store = _store()

    report = await _run(mcp, store)

    assert report.sessions_written == 0
    assert report.dropped["expiry_fetch_failed"] == 3


async def test_an_expiry_that_lists_nothing_is_recorded_not_raised() -> None:
    sessions = _sessions(3)
    mcp = _FakeMcp(sessions=sessions, no_contracts=True)
    store = _store()

    report = await _run(mcp, store)

    assert report.sessions_written == 0
    assert report.dropped["no_contracts_listed"] == 3


async def test_no_daily_bars_means_no_work_rather_than_a_crash() -> None:
    mcp = _FakeMcp(sessions=[])
    store = _store()

    report = await _run(mcp, store)

    assert report.sessions_missing == 0
    assert report.sessions_written == 0


async def test_the_tenor_stays_inside_the_backfill_band() -> None:
    """Monthly expiries only, so DTE walks — but never outside the band, or the
    series would be measuring a different option than it claims."""
    sessions = _sessions(140)
    mcp = _FakeMcp(sessions=sessions)
    store = _store()

    await _run(mcp, store)

    dtes = [row["dte"] for row in await store.daily_iv_history("SPY", days=252)]
    assert dtes
    assert min(dtes) >= 18
    assert max(dtes) <= 52
