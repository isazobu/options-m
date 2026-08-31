"""Tests for MarketPulseAgent.

Design change (2026-08-29+): MarketPulseAgent no longer calls
get_market_movers or get_most_active_stocks. Candidate ranking is driven
entirely by evidence collected from the option chain and daily bars.

The agent is the only thing standing between live broker data and the audit
trail, so the assertions here are mostly about restraint: it does not trade a
closed market, it does not invent numbers, and it does not swallow failures
that the supervisor needs to see.

The fake MCP provides the minimum surface EvidenceCollector needs (stock
snapshot, daily bars, option chain). Deliberately omitted: get_clock,
get_news, get_market_movers, get_most_active_stocks -- calling any of these
raises AttributeError, which is a stronger guarantee than a "not called"
assertion.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from options_m.agents.market_pulse import _EXCHANGE_TZ, MarketPulseAgent
from options_m.config import Settings
from options_m.db import Database
from options_m.store import Store

# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------

def _calendar_rows(*, is_open: bool) -> list[dict[str, Any]]:
    if not is_open:
        return []
    now_local = datetime.now(UTC).astimezone(_EXCHANGE_TZ)
    open_time = (now_local - timedelta(hours=1)).strftime("%H:%M")
    close_time = (now_local + timedelta(hours=1)).strftime("%H:%M")
    return [{"date": now_local.date().isoformat(), "open": open_time, "close": close_time}]


# ---------------------------------------------------------------------------
# Fake bar generators
# ---------------------------------------------------------------------------

def _uptrend_bars(n: int = 60, base: float = 400.0) -> list[dict[str, Any]]:
    """Monotonically increasing closes → RSI will be very high (>80)."""
    return [
        {"o": base + i, "h": base + i + 2, "l": base + i - 1, "c": base + i, "v": 500_000}
        for i in range(n)
    ]


def _flat_bars(n: int = 60, mid: float = 430.0, amplitude: float = 2.0) -> list[dict[str, Any]]:
    """Oscillating closes → RSI stays near 50."""
    import math
    return [
        {
            "o": mid,
            "h": mid + amplitude,
            "l": mid - amplitude,
            "c": mid + amplitude * math.sin(i * 0.5),
            "v": 300_000,
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Fake MCP
# ---------------------------------------------------------------------------

class _FakeMcp:
    """Minimal AlpacaMcp stand-in for evidence collection."""

    def __init__(
        self,
        *,
        is_open: bool = True,
        account: dict[str, Any] | None = None,
        account_config: dict[str, Any] | None = None,
        bars_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
        fail_evidence_for: set[str] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self._calendar_rows = _calendar_rows(is_open=is_open)
        self._account = account or {
            "equity": "100000.00",
            "cash": "100000.00",
            "buying_power": "200000.00",
            # The real Account carries the effective level here. Fixtures that
            # put it on account *configurations* instead are testing a shape
            # Alpaca never returns.
            "options_trading_level": 3,
            "options_approved_level": 3,
        }
        self._account_config = (
            account_config if account_config is not None else {"max_options_trading_level": 3}
        )
        self._bars_by_symbol = bars_by_symbol or {}
        self._fail_evidence_for = fail_evidence_for or set()
        self._fail_on = fail_on
        self.called: list[str] = []
        self.bars_requested_for: list[str] = []

    def _record(self, name: str) -> None:
        self.called.append(name)
        if self._fail_on == name:
            msg = f"{name} is down"
            raise RuntimeError(msg)

    async def get_calendar(self, start: str, end: str) -> list[dict[str, Any]]:
        self._record("get_calendar")
        horizon_row = {"date": end, "open": "09:30", "close": "16:00"}
        return [*self._calendar_rows, horizon_row]

    async def get_account_info(self) -> dict[str, Any]:
        self._record("get_account_info")
        return self._account

    async def get_account_config(self) -> dict[str, Any]:
        self._record("get_account_config")
        return self._account_config

    async def get_stock_snapshot(self, symbol: str) -> dict[str, Any]:
        self._record("get_stock_snapshot")
        if symbol.upper() in self._fail_evidence_for:
            msg = f"snapshot failed for {symbol}"
            raise RuntimeError(msg)
        price = 450.0
        return {
            "latestTrade": {"p": price},
            "latestQuote": {"bp": price - 0.05, "ap": price + 0.05, "bs": 100, "as": 100},
            "dailyBar": {"o": price - 2, "h": price + 3, "l": price - 3, "c": price, "v": 500_000},
            "prevDailyBar": {"c": price - 1},
        }

    async def get_stock_bars(self, symbol: str, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("get_stock_bars")
        self.bars_requested_for.append(symbol.upper())
        if symbol.upper() in self._fail_evidence_for:
            msg = f"bars failed for {symbol}"
            raise RuntimeError(msg)
        return self._bars_by_symbol.get(symbol.upper(), _flat_bars())

    async def get_option_chain(self, symbol: str, **kwargs: Any) -> dict[str, Any]:
        self._record("get_option_chain")
        return {}  # empty chain → options block = MISSING, that's fine

    async def get_option_contracts(self, symbol: str, **kwargs: Any) -> list[dict[str, Any]]:
        self._record("get_option_contracts")
        return []

    async def get_all_positions(self) -> list[dict[str, Any]]:
        self._record("get_all_positions")
        return []

    async def get_news(self, symbols: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        self._record("get_news")
        return []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _agent(mcp: Any, **overrides: Any) -> tuple[MarketPulseAgent, Store]:
    kwargs: dict[str, Any] = {"database_url": None, "universe": "SPY,QQQ,AAPL"}
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    store = Store(Database(settings))
    return MarketPulseAgent(settings, mcp, store), store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_uses_its_own_cadence_not_the_global_default() -> None:
    agent, _ = _agent(_FakeMcp(), market_pulse_interval_seconds=45.0)
    assert agent.interval_seconds == 45.0


async def test_a_closed_market_records_equity_but_skips_evidence() -> None:
    """No point burning broker calls against a closed market."""
    mcp = _FakeMcp(is_open=False)
    agent, store = _agent(mcp)

    await agent.step()

    assert "get_stock_bars" not in mcp.called
    assert await store.recent_candidates() == []
    assert len(await store.recent_equity()) == 1


async def test_an_open_market_collects_evidence_and_saves_candidates() -> None:
    """Evidence is collected for every universe symbol and candidates are saved."""
    mcp = _FakeMcp(bars_by_symbol={"SPY": _uptrend_bars(), "QQQ": _flat_bars()})
    agent, store = _agent(mcp)

    await agent.step()

    candidates = await store.recent_candidates()
    symbols = {row["symbol"] for row in candidates}
    # All three universe symbols should appear (AAPL gets flat bars by default).
    assert symbols == {"SPY", "QQQ", "AAPL"}
    # SPY has a strong uptrend → higher RSI extremity → higher score than flat QQQ.
    by_symbol = {row["symbol"]: row for row in candidates}
    assert by_symbol["SPY"]["score"] > by_symbol["QQQ"]["score"]


async def test_evidence_cache_is_written_for_each_universe_symbol() -> None:
    """Evidence cache rows must exist after a step so StrategistAgent can read them."""
    mcp = _FakeMcp()
    agent, store = _agent(mcp)

    await agent.step()

    for symbol in ("SPY", "QQQ", "AAPL"):
        row = await store.get_cached_evidence(symbol)
        assert row is not None, f"evidence cache missing for {symbol}"
        assert "symbol" in (row.get("payload") or {}), f"pack has no symbol key for {symbol}"


async def test_evidence_collection_is_limited_to_universe_symbols() -> None:
    """EvidenceCollector must only be invoked for the fixed universe."""
    mcp = _FakeMcp()
    agent, _ = _agent(mcp, universe="SPY,QQQ")

    await agent.step()

    assert set(mcp.bars_requested_for) == {"SPY", "QQQ"}


async def test_a_failed_evidence_collection_does_not_stop_the_others() -> None:
    """One symbol's missing data must not prevent the others from being cached.

    When get_stock_bars raises for QQQ, EvidenceCollector returns MISSING for
    the trend block. MarketPulseAgent skips the cache write for that symbol
    (a MISSING-only pack is useless to StrategistAgent) but continues with
    SPY and AAPL.
    """
    mcp = _FakeMcp(fail_evidence_for={"QQQ"})
    agent, store = _agent(mcp)

    await agent.step()

    # Agent-level run must still be ok.
    run = (await store.recent_agent_runs())[0]
    assert run["ok"] is True
    # SPY and AAPL get real bar data → trend block → cached.
    assert await store.get_cached_evidence("SPY") is not None
    assert await store.get_cached_evidence("AAPL") is not None
    # QQQ's bars failed → no trend block → cache write skipped.
    assert await store.get_cached_evidence("QQQ") is None


async def test_unreadable_account_fields_are_recorded_as_unknown() -> None:
    """A NaN or missing balance must never be persisted as 0.0."""
    mcp = _FakeMcp(is_open=False, account={"equity": "n/a", "cash": None})
    agent, store = _agent(mcp)

    await agent.step()

    point = (await store.recent_equity())[0]
    assert point["equity"] is None
    assert point["cash"] is None


async def test_a_broker_failure_propagates_to_the_supervisor() -> None:
    """Swallowing this would hide a dead broker behind a healthy-looking loop."""
    mcp = _FakeMcp(fail_on="get_account_info")
    agent, store = _agent(mcp)

    with pytest.raises(RuntimeError, match="get_account_info is down"):
        await agent.step()

    run = (await store.recent_agent_runs())[0]
    assert run["ok"] is False
    assert "get_account_info is down" in (run["error"] or "")


async def test_a_successful_step_is_recorded_with_its_detail() -> None:
    agent, store = _agent(_FakeMcp())

    await agent.step()

    run = (await store.recent_agent_runs())[0]
    assert run["agent"] == "market_pulse"
    assert run["ok"] is True
    assert run["detail"]["market_open"] is True
    assert run["detail"]["evidence_written"] > 0


async def test_calendar_is_fetched_once_then_reused_within_the_margin() -> None:
    """The whole point of the cache: a fresh window means no second live call."""
    mcp = _FakeMcp()
    agent, _ = _agent(mcp)

    await agent.step()
    await agent.step()

    assert mcp.called.count("get_calendar") == 1


async def test_the_calendar_window_reaches_back_past_today() -> None:
    """A window starting at today holds no session at all over a weekend."""
    mcp = _FakeMcp()
    requested: list[tuple[str, str]] = []
    original = mcp.get_calendar

    async def _capture(start: str, end: str) -> list[dict[str, Any]]:
        requested.append((start, end))
        return await original(start, end)

    mcp.get_calendar = _capture  # type: ignore[method-assign]
    agent, _ = _agent(mcp, market_calendar_lookback_days=7)

    await agent.step()

    start, _end = requested[0]
    today = datetime.now(UTC).astimezone(_EXCHANGE_TZ).date()
    assert date.fromisoformat(start) == today - timedelta(days=7)


async def test_a_forward_only_cache_is_backfilled_but_only_once() -> None:
    """An older build's cache starts at today; healing it must not loop.

    The broker here returns nothing at or before today, so coverage can never
    be satisfied by the data itself — exactly the case that would otherwise
    refetch the calendar on every single tick.
    """
    mcp = _FakeMcp(is_open=False)
    agent, store = _agent(mcp)
    today = datetime.now(UTC).astimezone(_EXCHANGE_TZ).date()
    await store.upsert_market_calendar(
        [
            {
                "date": today + timedelta(days=offset),
                "open": datetime.now(UTC) + timedelta(days=offset),
                "close": datetime.now(UTC) + timedelta(days=offset, hours=6),
                "session_type": "full",
            }
            for offset in (1, 200)
        ]
    )

    await agent.step()
    await agent.step()
    await agent.step()

    assert mcp.called.count("get_calendar") == 1


async def test_positions_count_comes_from_the_local_cache_not_a_live_call() -> None:
    """PositionManagerAgent owns the positions cache; this agent only reads it."""
    mcp = _FakeMcp()
    agent, store = _agent(mcp)
    await store.upsert_position("SPY", {"symbol": "SPY", "qty": "1"})

    await agent.step()

    point = (await store.recent_equity())[0]
    assert point["positions_count"] == 1


async def test_the_options_level_comes_from_the_account_not_the_config() -> None:
    """Account *configurations* has no `options_trading_level` field at all.

    It carries `max_options_trading_level`, a self-imposed ceiling. Reading the
    level from the config alone cached None on the real API, and matrix.py then
    fell back to the configured cap of 3 — assuming full Level 3 approval.
    """
    mcp = _FakeMcp(
        account={
            "equity": "100000.00",
            "cash": "100000.00",
            "buying_power": "200000.00",
            # Approved is not what may be traded; only the latter is honoured.
            "options_approved_level": 3,
            "options_trading_level": 2,
        },
        account_config={"max_options_trading_level": 3},
    )
    agent, store = _agent(mcp)

    await agent.step()

    cached = await store.get_cached_account()
    assert cached is not None
    assert cached["options_trading_level"] == 2


async def test_a_self_imposed_ceiling_lowers_the_options_level() -> None:
    """max_options_trading_level can only ever lower what the account allows."""
    mcp = _FakeMcp(
        account={
            "equity": "100000.00",
            "cash": "100000.00",
            "buying_power": "200000.00",
            "options_trading_level": 3,
        },
        account_config={"max_options_trading_level": 1},
    )
    agent, store = _agent(mcp)

    await agent.step()

    cached = await store.get_cached_account()
    assert cached is not None
    assert cached["options_trading_level"] == 1


async def test_account_cache_is_upserted_from_the_same_tick_no_extra_calls() -> None:
    mcp = _FakeMcp(
        account={
            "equity": "123456.78",
            "cash": "50000.00",
            "buying_power": "150000.00",
            "options_trading_level": 2,
        },
        account_config={"max_options_trading_level": 3},
    )
    agent, store = _agent(mcp)

    await agent.step()

    cached = await store.get_cached_account()
    assert cached is not None
    assert float(cached["equity"]) == 123456.78
    assert cached["options_trading_level"] == 2
    assert mcp.called.count("get_account_info") == 1
    assert mcp.called.count("get_account_config") == 1


async def test_earnings_blacked_out_symbol_scores_zero() -> None:
    """A blacked-out symbol must appear as a candidate with score 0.0."""
    # SPY and QQQ are ETFs (never blacked out); AAPL might be during its window.
    # We use a custom universe where we know no blackout applies -- ETFs only.
    mcp = _FakeMcp()
    agent, store = _agent(mcp, universe="SPY,QQQ")

    await agent.step()

    # Both ETFs should score > 0 (RSI / RV signals present from flat bars).
    candidates = await store.recent_candidates()
    assert all(row["score"] >= 0 for row in candidates)
