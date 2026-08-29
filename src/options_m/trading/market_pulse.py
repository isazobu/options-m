"""MarketPulseAgent — the service's senses.

Runs every minute. It answers three questions and writes the answers down:
is the market open, what is the account worth, and which symbols in our universe
are worth a closer look. Everything downstream reads from what this agent
persists, which is why it holds no opinions of its own and calls no LLM.

Market state comes from the broker clock and nothing else. Hardcoded market
calendars go stale, and a holiday list that is wrong by one day means the agent
trades into a closed market or sits out an open one.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from options_m.config import Settings
from options_m.mcp_client import AlpacaMcp, finite_float
from options_m.store import Store

logger = logging.getLogger(__name__)


class MarketPulseAgent:
    """Account and market telemetry, plus a deterministic candidate watchlist."""

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
            # Telemetry is written even on failure — a run that keeps failing is
            # exactly the run the agent-health panel needs to show.
            await self._store.record_agent_run(
                self.name,
                duration_ms=int((time.monotonic() - started) * 1000),
                ok=ok,
                error=error,
                detail=detail or None,
            )

    async def _run(self) -> dict[str, Any]:
        clock = await self._mcp.get_clock()
        is_open = bool(clock.get("is_open"))

        account = await self._mcp.get_account_info()
        positions = await self._mcp.get_all_positions()

        await self._store.append_equity(
            equity=finite_float(account.get("equity")),
            cash=finite_float(account.get("cash")),
            buying_power=finite_float(account.get("buying_power")),
            positions_count=len(positions),
        )

        detail: dict[str, Any] = {
            "market_open": is_open,
            "next_open": clock.get("next_open"),
            "next_close": clock.get("next_close"),
            "positions": len(positions),
            "equity": finite_float(account.get("equity")),
        }

        if not is_open:
            # Burning API calls and Neon compute against a closed market buys
            # nothing. The equity point above is enough to keep the curve honest.
            logger.info("market closed", extra=detail)
            detail["candidates"] = 0
            return detail

        movers = await self._mcp.get_market_movers()
        actives = await self._mcp.get_most_active_stocks()
        news = await self._mcp.get_news(self._universe, limit=20)

        candidates = self._score(movers, actives, news)
        await self._store.save_market_snapshot(
            {"clock": clock, "movers": movers, "actives": actives}
        )
        await self._store.save_candidates(candidates)

        detail["candidates"] = len(candidates)
        logger.info("market pulse", extra=detail)
        return detail

    def _score(self, movers: Any, actives: Any, news: Any) -> list[dict[str, Any]]:
        """Rank universe symbols deterministically. No model, no randomness.

        Three additive signals: absolute percentage move, presence on the
        most-active list, and recent headline count. The absolute value matters —
        a symbol down 4% is as interesting to an options agent as one up 4%.
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

        headline_counts = _count_headlines(news, scores)
        for symbol, count in headline_counts.items():
            scores[symbol] += min(count, 5) * 0.5
            reasons[symbol].append(f"{count} headlines")
            payloads[symbol]["headline_count"] = count

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


def _count_headlines(news: Any, known: dict[str, float]) -> dict[str, int]:
    counts: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    if isinstance(news, list):
        items = [item for item in news if isinstance(item, dict)]
    elif isinstance(news, dict):
        for key in ("news", "data", "results"):
            value = news.get(key)
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))

    for item in items:
        symbols = item.get("symbols")
        if not isinstance(symbols, list):
            continue
        for raw in symbols:
            symbol = str(raw).upper()
            if symbol in known:
                counts[symbol] = counts.get(symbol, 0) + 1
    return counts
