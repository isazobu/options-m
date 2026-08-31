"""MarketPulseAgent -- the service's senses.

Runs every minute. Three jobs, all performed without an LLM:

1. Keep the local market_calendar cache current (get_calendar once, refresh
   when the rolling window shrinks below the margin).
2. Keep the account cache current (equity, cash, buying_power, options level)
   and append one equity-curve point per tick.
3. For every symbol in the fixed universe: collect the full evidence pack
   (trend from daily bars, IV/RV regime from the option chain, earnings-
   blackout flag), write it to the local evidence cache, and derive a
   deterministic candidate score from those signals so StrategistAgent can
   rank symbols without calling any MCP tool itself.

Candidate ranking is driven entirely by technical evidence (IV/RV ratio, RSI
extremity, realised volatility) — the same signals the Strategy Matrix uses.

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

from options_m import session
from options_m.config import Settings
from options_m.earnings import is_earnings_blackout
from options_m.evidence.evidence import EvidenceCollector
from options_m.iv_backfill import backfill_daily_iv
from options_m.mcp_client import AlpacaMcp, finite_float
from options_m.store import Store

logger = logging.getLogger(__name__)

_EXCHANGE_TZ = ZoneInfo("America/New_York")

# IV/RV thresholds for the candidate score (mirror matrix.py, but kept local
# so this module has no import dependency on the reasoning layer).
_IV_RV_EDGE_THRESHOLD = 1.05   # any premium above this scores positively
_RSI_NEUTRAL = 50.0


class MarketPulseAgent:
    """Market-calendar + account telemetry + evidence-driven candidate ranking."""

    def __init__(self, settings: Settings, mcp: AlpacaMcp, store: Store) -> None:
        self._settings = settings
        self._mcp = mcp
        self._store = store
        self._universe = settings.universe_symbols
        self._evidence = EvidenceCollector(settings, mcp, store)
        # Set once the calendar has been fetched with the backward window; see
        # _ensure_calendar_fresh.
        self._backfilled = False
        # Symbols whose IV history this process has already tried to
        # reconstruct. One attempt each: the pass is incremental against what
        # iv_history already holds, so a symbol with sessions that simply
        # cannot be inverted (no prints on its ATM strike) would otherwise be
        # refetched every minute forever.
        self._iv_backfill_attempted: set[str] = set()

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
        state = await session.current(self._store, self._settings, now)
        is_open = state.is_open

        account = await self._mcp.get_account_info()
        account_config = await self._mcp.get_account_config()
        options_level = _parse_options_level(account, account_config)

        equity = finite_float(account.get("equity"))
        cash = finite_float(account.get("cash"))
        buying_power = finite_float(account.get("buying_power"))

        await self._store.upsert_account(
            equity=equity,
            cash=cash,
            buying_power=buying_power,
            options_trading_level=options_level,
        )

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
            "session_replayed": state.replayed,
            "calendar_refreshed": calendar_refreshed,
            "positions": positions_count,
            "equity": equity,
            "options_trading_level": options_level,
        }

        if not is_open:
            logger.info("market closed", extra=detail)
            detail["candidates"] = 0
            detail["evidence_written"] = 0
            return detail

        # Reconstruct missing IV history before the evidence loop, so this
        # tick's pack already ranks against the deeper window. Kept behind the
        # market-open gate above with every other market-data read: the bars it
        # asks for are historical, but the rule that a closed market costs no
        # broker calls is worth more than finishing the backfill overnight.
        iv_backfill = await self._backfill_iv_history()
        if iv_backfill:
            detail["iv_backfill"] = iv_backfill

        # Collect evidence for every universe symbol, derive scores, persist.
        # Each symbol is independent: a failure on one does not stop the rest.
        today = now.astimezone(_EXCHANGE_TZ).date()
        candidates: list[dict[str, Any]] = []
        evidence_written = 0

        for symbol in self._universe:
            try:
                pack = await self._evidence.collect(
                    symbol,
                    dte_min=self._settings.risk_dte_min,
                    dte_max=self._settings.risk_dte_max,
                    iv_dte_min=self._settings.dte_target_min,
                    iv_dte_max=self._settings.dte_target_max,
                )
                # Augment with fields StrategistAgent needs so it can classify
                # trend + IV regime without reading the raw indicators itself.
                pack["earnings_blackout"] = is_earnings_blackout(symbol, today)
                if isinstance(options_level, int):
                    pack["options_trading_level"] = options_level

                # Only cache packs that have at least a trend block with real
                # data. A pack where every section is MISSING is indistinguishable
                # from a stale row and gives StrategistAgent nothing to reason
                # from -- skipping it is safer than caching an empty shell.
                if not isinstance(pack.get("trend"), dict):
                    logger.warning(
                        "evidence pack has no trend data, skipping cache write",
                        extra={"symbol": symbol},
                    )
                    continue

                await self._store.upsert_evidence_cache(symbol, pack)
                evidence_written += 1

                score, reason = _score_from_evidence(pack)
                candidates.append(
                    {
                        "symbol": symbol,
                        "score": score,
                        "reason": reason,
                        "payload": {},
                    }
                )
            except Exception:
                logger.warning(
                    "evidence collection failed",
                    extra={"symbol": symbol},
                    exc_info=True,
                )

        candidates.sort(key=lambda r: float(r["score"]), reverse=True)
        if candidates:
            await self._store.save_candidates(candidates)

        detail["candidates"] = len(candidates)
        detail["evidence_written"] = evidence_written
        logger.info("market pulse", extra=detail)
        return detail

    async def _backfill_iv_history(self) -> list[dict[str, Any]]:
        """Reconstruct missing daily ATM-IV sessions for a few symbols.

        IV Rank needs a trading year of daily observations before it means
        anything, and the live writer takes a year to produce one. This pulls
        the missing sessions out of Alpaca's historical option bars instead —
        see :mod:`options_m.iv_backfill` for what that price is and is not.

        Paced at ``iv_backfill_symbols_per_tick`` symbols per tick because each
        symbol costs a handful of Alpaca calls; a ten-symbol universe is
        therefore covered within the first ten minutes of a run. A failure is
        logged and dropped: the evidence pack degrades to a MISSING rank, which
        is the same state the service was already in.
        """
        if not self._settings.iv_backfill_enabled:
            return []
        pending = [
            symbol for symbol in self._universe if symbol not in self._iv_backfill_attempted
        ]
        if not pending:
            return []

        reports: list[dict[str, Any]] = []
        for symbol in pending[: self._settings.iv_backfill_symbols_per_tick]:
            self._iv_backfill_attempted.add(symbol)
            try:
                report = await backfill_daily_iv(
                    self._settings, self._mcp, self._store, symbol
                )
            except Exception:
                logger.warning(
                    "iv history backfill failed",
                    extra={"symbol": symbol},
                    exc_info=True,
                )
                continue
            if report.sessions_missing:
                reports.append(report.as_detail())
        return reports

    async def _ensure_calendar_fresh(self) -> bool:
        """Populate or extend the local market_calendar cache.

        The window reaches backwards as well as forwards. The forward half is
        what every market-open check reads; the backward half exists because a
        cache that starts at today holds no session at all on a weekend, which
        leaves "when did the market last trade" unanswerable — the question
        REPLAY_LAST_SESSION asks, and a useful one for any later look-back.
        """
        today = datetime.now(UTC).astimezone(_EXCHANGE_TZ).date()
        start = today - timedelta(days=self._settings.market_calendar_lookback_days)
        end = today + timedelta(days=self._settings.market_calendar_horizon_days)

        max_date = await self._store.calendar_max_date()
        min_date = await self._store.calendar_min_date()
        margin = timedelta(days=self._settings.market_calendar_refresh_margin_days)
        forward_covered = max_date is not None and max_date - today > margin
        # A cache written by a forward-only build covers the future but starts
        # at today, so it holds no past session; refetching heals it. The
        # coverage test is "any session at or before today", not "a row at
        # start" — start is a calendar day and may well not be a trading one.
        # ``_backfilled`` then bounds this to one extra fetch per process, so a
        # broker that returns nothing before today cannot turn the healing path
        # into a per-tick refetch loop.
        backward_covered = min_date is not None and (min_date <= today or self._backfilled)
        if forward_covered and backward_covered:
            return False

        raw_rows = await self._mcp.get_calendar(start.isoformat(), end.isoformat())
        self._backfilled = True
        rows = [_parse_calendar_entry(entry) for entry in raw_rows]
        await self._store.upsert_market_calendar(rows)
        logger.info(
            "market calendar refreshed",
            extra={"rows": len(rows), "from": start.isoformat(), "through": end.isoformat()},
        )
        return True


# ---------------------------------------------------------------------------
# Evidence-driven candidate scoring
# ---------------------------------------------------------------------------

def _score_from_evidence(pack: dict[str, Any]) -> tuple[float, str]:
    """Derive a deterministic candidate score from the evidence pack.

    Higher score = more interesting to StrategistAgent. The signals:

    - IV/RV edge: how much implied vol exceeds realised vol. The further above
      1.0, the more premium is available to collect (or the clearer the signal
      that options are mispriced relative to historical move).
    - RSI extremity: how far RSI14 is from neutral (50). A strong trend in
      either direction is more actionable than a flat oscillation.
    - Realised vol: a baseline measure of how much the underlying moves; higher
      RV means options strategies have more room for premium or spread width.

    Earnings-blacked-out symbols score 0.0 so they sink below every
    tradeable symbol without being excluded from the evidence cache.
    """
    if pack.get("earnings_blackout"):
        return 0.0, "earnings_blackout"

    score = 0.0
    reasons: list[str] = []

    trend = pack.get("trend")
    options = pack.get("options")

    # RSI extremity component
    if isinstance(trend, dict):
        rsi = trend.get("rsi_14")
        if isinstance(rsi, (int, float)):
            extremity = abs(rsi - _RSI_NEUTRAL)
            if extremity >= 10:
                rsi_score = round(extremity / 10, 3)
                score += rsi_score
                reasons.append(f"rsi {rsi:.1f}")

        rv = trend.get("realised_vol_20d")
        if isinstance(rv, (int, float)) and rv > 0:
            rv_score = round(min(rv * 2, 1.5), 3)
            score += rv_score
            reasons.append(f"rv {rv:.1%}")

    # IV/RV edge component
    if isinstance(options, dict) and isinstance(trend, dict):
        iv_atm = options.get("iv_atm")
        rv = trend.get("realised_vol_20d")
        if (
            isinstance(iv_atm, (int, float))
            and isinstance(rv, (int, float))
            and rv > 0
        ):
            ratio = iv_atm / rv
            if ratio > _IV_RV_EDGE_THRESHOLD:
                edge_score = round(min((ratio - 1.0) * 3, 3.0), 3)
                score += edge_score
                reasons.append(f"iv/rv {ratio:.2f}")

    return round(score, 4), ", ".join(reasons)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_options_level(
    account: dict[str, Any], account_config: dict[str, Any]
) -> int | None:
    """Effective options trading level, never the merely-approved one.

    The level lives on the *account* (`options_trading_level`, alongside the
    merely-approved `options_approved_level` which is not what may be traded).
    Account *configurations* carries only `max_options_trading_level`, a
    self-imposed ceiling — which is why reading the level from the config alone
    returned None against the real API and every pulse logged
    `options_trading_level: null`. matrix.py then fell back to the configured
    cap of 3, so the service assumed full Level 3 approval it may not have.

    Both are honoured when present: the ceiling can only lower the level.
    """
    levels = [
        parsed
        for parsed in (
            _int_or_none(account.get("options_trading_level")),
            _int_or_none(account_config.get("max_options_trading_level")),
        )
        if parsed is not None
    ]
    return min(levels) if levels else None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_calendar_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalise one get_calendar row into {date, open, close, session_type}."""
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
