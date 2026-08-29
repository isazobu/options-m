"""MarketPulseAgent -- the service's senses.

Runs every minute. It answers three questions and writes the answers down:
is the market open, what is the account worth, and which symbols in our
universe are worth a closer look. Everything downstream reads from what this
agent persists, which is why it holds no opinions of its own and calls no LLM.

Design change (2026-08-29): market state and account state both moved from a
live broker call on every check to a local Postgres cache that this agent
alone writes. Concretely:

- Market state used to come from get_clock on every single check across every
  agent. It now comes from a market_calendar table this agent populates from
  get_calendar once at startup (a ~1yr forward window) and refreshes once the
  window shrinks under a configured margin. store.market_is_open() is the one
  function every agent's "is the market open" check calls; get_clock is kept
  on AlpacaMcp only as an optional sanity check, never in the per-iteration
  hot path.
- Account state (equity, cash, buying power, options trading level) is now
  also cached locally, upserted here on the same get_account_info /
  get_account_config call this agent already made every tick for
  equity_curve -- no new Alpaca traffic. ExecutionAgent (Phase 2) reads the
  cache instead of calling get_account_info itself.
- News is dropped entirely from this agent's candidate scoring -- the system
  no longer reads news anywhere (see docs/plan/00-MASTER.md).

Accepted risk, stated explicitly: a once-a-day calendar refresh will not
catch an unscheduled intraday circuit-breaker halt. Acceptable for this
project's short run; would need revisiting for a longer-lived or
real-capital deployment.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as _time
from typing import Any
from zoneinfo import ZoneInfo

from options_m.config import Settings
from options_m.mcp_client import AlpacaMcp, finite_float
from options_m.store import Store

logger = logging.getLogger(__name__)

# Alpaca's calendar entries are expressed in the exchange's own local time,
# not UTC -- combine date + "HH:MM" here, in this zone, before converting.
_EXCHANGE_TZ = ZoneInfo("America/New_York")


class MarketPulseAgent:
    """Account and market-calendar telemetry, plus a deterministic candidate watchlist."""

    def __init__(self, settings: Settings, mcp: AlpacaMcp, store: Store) -> None:
        self._settings = settings
        self._mcp = mcp
        self._store = store
        self._universe = settings.universe_symbols

    @property
    def name(self) -> str:
        return "market_pulse"

    @property
    def interval_seconds(self) -> float:
        return self._settings.market_pulse_interval_seconds

    async def step(self) -> None:
        """One iteration. Raises on failure so the supervisor can back off."""
        started = time.monotonic()
        ok = True
        error: str | None = None
        detail: dict[str, Any] = {}
        try:
            detail = await self._run()
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            # Telemetry is written even on failure -- a run that keeps failing is
            # exactly the run the agent-health panel needs to show.
            await self._store.record_agent_run(
                self.name,
                duration_ms=int((time.monotonic() - started) * 1000),
                ok=ok,
                error=error,
                detail=detail or None,
            )

    async def _run(self) -> dict[str, Any]:
        calendar_refreshed = await self._ensure_calendar_fresh()

        now = datetime.now(UTC)
        is_open = await self._store.market_is_open(now)

        account = await self._mcp.get_account_info()
        account_config = await self._mcp.get_account_config()
        options_level = _parse_options_level(account_config)

        equity = finite_float(account.get("equity"))
        cash = finite_float(account.get("cash"))
        buying_power = finite_float(account.get("buying_power"))

        await self._store.upsert_account(
            equity=equity,
            cash=cash,
            buying_power=buying_power,
            options_trading_level=options_level,
        )

        # positions_count comes from the local cache PositionManagerAgent owns,
        # never from a second live get_all_positions call here -- that would
        # defeat the point of giving that cache a single writer.
        cached_positions = await self._store.get_cached_positions()
        positions_count = len(cached_positions)

        await self._store.append_equity(
            equity=equity,
            cash=cash,
            buying_power=buying_power,
            positions_count=positions_count,
        )

        detail: dict[str, Any] = {
            "market_open": is_open,
            "calendar_refreshed": calendar_refreshed,
            "positions": positions_count,
            "equity": equity,
            "options_trading_level": options_level,
        }

        if not is_open:
            # Burning API calls and Neon compute against a closed market buys
            # nothing. The equity point above is enough to keep the curve honest.
            logger.info("market closed", extra=detail)
            detail["candidates"] = 0
            return detail

        movers = await self._mcp.get_market_movers()
        actives = await self._mcp.get_most_active_stocks()

        candidates = self._score(movers, actives)
        await self._store.save_market_snapshot({"movers": movers, "actives": actives})
        await self._store.save_candidates(candidates)

        detail["candidates"] = len(candidates)
        logger.info("market pulse", extra=detail)
        return detail

    async def _ensure_calendar_fresh(self) -> bool:
        """Populate or extend the local market_calendar cache.

        Returns whether a fetch happened this tick (mostly for telemetry/tests).
        """
        today = datetime.now(UTC).astimezone(_EXCHANGE_TZ).date()
        max_date = await self._store.calendar_max_date()
        margin = timedelta(days=self._settings.market_calendar_refresh_margin_days)
        if max_date is not None and max_date - today > margin:
            return False

        horizon = timedelta(days=self._settings.market_calendar_horizon_days)
        end = today + horizon
        raw_rows = await self._mcp.get_calendar(today.isoformat(), end.isoformat())
        rows = [_parse_calendar_entry(entry) for entry in raw_rows]
        await self._store.upsert_market_calendar(rows)
        logger.info(
            "market calendar refreshed",
            extra={"rows": len(rows), "through": end.isoformat()},
        )
        return True

    def _score(self, movers: Any, actives: Any) -> list[dict[str, Any]]:
        """Rank universe symbols deterministically. No model, no randomness.

        Two additive signals now that news is gone: absolute percentage move,
        and presence on the most-active list. The absolute value matters -- a
        symbol down 4% is as interesting to an options agent as one up 4%.
        """
        scores: dict[str, float] = dict.fromkeys(self._universe, 0.0)
        reasons: dict[str, list[str]] = {symbol: [] for symbol in self._universe}
        payloads: dict[str, dict[str, Any]] = {symbol: {} for symbol in self._universe}

        for entry in _iter_symbol_entries(movers):
            symbol = str(entry.get("symbol", "")).upper()
            if symbol not in scores:
                continue
            change = finite_float(entry.get("percent_change"))
            if change is None:
                continue
            scores[symbol] += abs(change)
            reasons[symbol].append(f"moved {change:+.2f}%")
            payloads[symbol]["percent_change"] = change

        for entry in _iter_symbol_entries(actives):
            symbol = str(entry.get("symbol", "")).upper()
            if symbol not in scores:
                continue
            scores[symbol] += 1.0
            reasons[symbol].append("most active")
            volume = finite_float(entry.get("volume"))
            if volume is not None:
                payloads[symbol]["volume"] = volume

        ranked = [
            {
                "symbol": symbol,
                "score": round(score, 4),
                "reason": ", ".join(reasons[symbol]),
                "payload": payloads[symbol],
            }
            for symbol, score in scores.items()
            if score > 0
        ]
        ranked.sort(key=lambda row: float(row["score"]), reverse=True)  # type: ignore[arg-type]
        return ranked


def _parse_options_level(account_config: dict[str, Any]) -> int | None:
    """Effective options trading level, never the merely-approved one.

    Field naming has varied across Alpaca API versions; check the effective
    field first and only fall back to an approved-level field if it is truly
    the only one present, since gating on the wrong one is a real safety bug
    (Phase 2's risk engine and Phase 3's Level-2 downgrade both depend on
    this being the *effective* level).
    """
    for key in ("options_trading_level", "options_trading_level_effective"):
        value = account_config.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _parse_calendar_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalise one get_calendar row into {date, open, close, session_type}.

    Alpaca's calendar entries carry a date plus "HH:MM" open/close strings in
    the exchange's own local time -- never UTC and never just a bare time.
    Raise rather than guess on a malformed entry: a silently-skipped trading
    day is exactly the kind of gap that makes the market-open cache lie.
    """
    date_str = entry.get("date")
    open_str = entry.get("open")
    close_str = entry.get("close")
    if (
        not isinstance(date_str, str)
        or not isinstance(open_str, str)
        or not isinstance(close_str, str)
    ):
        msg = f"calendar entry missing date/open/close: {entry!r}"
        raise ValueError(msg)

    day = date.fromisoformat(date_str)
    open_dt = datetime.combine(day, _parse_hhmm(open_str), tzinfo=_EXCHANGE_TZ)
    close_dt = datetime.combine(day, _parse_hhmm(close_str), tzinfo=_EXCHANGE_TZ)
    return {
        "date": day,
        "open": open_dt.astimezone(UTC),
        "close": close_dt.astimezone(UTC),
        "session_type": "full",
    }


def _parse_hhmm(value: str) -> _time:
    hour_str, _, minute_str = value.partition(":")
    return _time(hour=int(hour_str), minute=int(minute_str))


def _iter_symbol_entries(payload: Any) -> list[dict[str, Any]]:
    """Pull symbol rows out of a mover/active payload of uncertain shape.

    Returns an empty list rather than raising: an unfamiliar shape here costs us
    a scoring signal, not correctness. Anything that feeds an order goes through
    a strict path instead.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    entries: list[dict[str, Any]] = []
    for key in ("gainers", "losers", "most_actives", "actives", "data", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            entries.extend(item for item in value if isinstance(item, dict))
    return entries
