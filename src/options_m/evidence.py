"""Deterministic evidence collection for one underlying. No LLM.

Every sub-fetch is wrapped individually so one failure (a down news feed, a
thin bar history) costs one field, never the whole pack — this is evidence
for reasoning and dashboard display, not a money-moving path. Contrast
:mod:`options_m.strategy_builder`, which is strict everywhere a bad read
could produce an order.

Any field genuinely unavailable becomes the literal string
``"NO_DATA_AVAILABLE"``, with a top-level note forbidding estimation —
borrowed from ``TradingAgents/tradingagents/dataflows/interface.py:242``, and
the only thing standing between a quiet gap in the data and an invented IV
number three modules later.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections.abc import Awaitable
from datetime import date, timedelta
from typing import Any

from options_m.config import Settings
from options_m.mcp_client import AlpacaMcp, finite_float
from options_m.store import Store
from options_m.strategy_builder import NormalizedContract, normalize_contracts

logger = logging.getLogger(__name__)

NO_DATA = "NO_DATA_AVAILABLE"
NOTE = (
    "Fields marked NO_DATA_AVAILABLE are genuinely unavailable. "
    "Do not estimate or fabricate values."
)

_NEWS_TRUNCATE = 280


async def _safe(field: str, symbol: str, awaitable: Awaitable[Any]) -> Any:
    try:
        return await awaitable
    except Exception:
        logger.warning(
            "evidence sub-fetch failed; recording as unavailable",
            extra={"field": field, "symbol": symbol},
            exc_info=True,
        )
        return NO_DATA


def _sma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _rsi14(closes: list[float]) -> float | None:
    if len(closes) < 15:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(len(closes) - 14, len(closes))]
    gains = [c for c in changes if c > 0]
    losses = [-c for c in changes if c < 0]
    avg_gain = sum(gains) / 14
    avg_loss = sum(losses) / 14
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _atr14(bars: list[dict[str, Any]]) -> float | None:
    if len(bars) < 15:
        return None
    true_ranges: list[float] = []
    for i in range(len(bars) - 14, len(bars)):
        high, low, prev_close = bars[i].get("h"), bars[i].get("l"), bars[i - 1].get("c")
        if high is None or low is None or prev_close is None:
            return None
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(true_ranges) / len(true_ranges)


def _realized_vol_20d(closes: list[float]) -> float | None:
    if len(closes) < 21:
        return None
    window = closes[-21:]
    log_returns = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
    if len(log_returns) < 2:
        return None
    return statistics.stdev(log_returns) * math.sqrt(252)


def _indicators(bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [c for bar in bars if (c := finite_float(bar.get("c"))) is not None]
    highs = [h for bar in bars if (h := finite_float(bar.get("h"))) is not None]
    lows = [l for bar in bars if (l := finite_float(bar.get("l"))) is not None]  # noqa: E741

    rsi14, atr14, realized_vol = _rsi14(closes), _atr14(bars), _realized_vol_20d(closes)
    result: dict[str, Any] = {
        "sma20": _sma(closes, 20) or NO_DATA,
        "sma50": _sma(closes, 50) or NO_DATA,
        "rsi14": rsi14 if rsi14 is not None else NO_DATA,
        "atr14": atr14 if atr14 is not None else NO_DATA,
        "realized_vol_20d": realized_vol if realized_vol is not None else NO_DATA,
    }
    # Labeled honestly as the fetched window (not "52-week") — get_stock_bars
    # is called with a fixed limit, well short of a year of trading days.
    if closes and highs and lows:
        last_close, period_high, period_low = closes[-1], max(highs), min(lows)
        result["pct_from_period_high"] = (
            (last_close - period_high) / period_high if period_high else NO_DATA
        )
        result["pct_from_period_low"] = (
            (last_close - period_low) / period_low if period_low else NO_DATA
        )
    else:
        result["pct_from_period_high"] = NO_DATA
        result["pct_from_period_low"] = NO_DATA
    return result


def _atm_iv(contracts: list[NormalizedContract], spot: float, option_type: str) -> float | None:
    same_type = [c for c in contracts if c.option_type == option_type and c.implied_volatility]
    if not same_type:
        return None
    nearest = min(same_type, key=lambda c: abs(c.strike - spot))
    return nearest.implied_volatility


def _options_summary(
    contracts_raw: Any, snapshots_raw: Any, spot: float | None
) -> dict[str, Any]:
    if contracts_raw == NO_DATA or snapshots_raw == NO_DATA or spot is None:
        return {
            "iv_atm": NO_DATA,
            "put_call_skew": NO_DATA,
            "term_structure": NO_DATA,
            "median_spread_pct": NO_DATA,
            "total_open_interest": NO_DATA,
        }

    contracts = normalize_contracts(contracts_raw, snapshots_raw)
    if not contracts:
        return {
            "iv_atm": NO_DATA,
            "put_call_skew": NO_DATA,
            "term_structure": NO_DATA,
            "median_spread_pct": NO_DATA,
            "total_open_interest": NO_DATA,
        }

    call_iv = _atm_iv(contracts, spot, "call")
    put_iv = _atm_iv(contracts, spot, "put")
    if call_iv is not None and put_iv is not None:
        iv_atm: Any = (call_iv + put_iv) / 2
        put_call_skew: Any = put_iv - call_iv
    elif call_iv is not None:
        iv_atm, put_call_skew = call_iv, NO_DATA
    elif put_iv is not None:
        iv_atm, put_call_skew = put_iv, NO_DATA
    else:
        iv_atm, put_call_skew = NO_DATA, NO_DATA

    expiries = sorted({c.expiry for c in contracts})
    term_structure: Any = NO_DATA
    if len(expiries) >= 2:
        near_atm = _atm_iv([c for c in contracts if c.expiry == expiries[0]], spot, "call")
        far_atm = _atm_iv([c for c in contracts if c.expiry == expiries[1]], spot, "call")
        if near_atm is not None and far_atm is not None:
            term_structure = far_atm - near_atm

    spreads = [
        (c.ask - c.bid) / ((c.ask + c.bid) / 2)
        for c in contracts
        if c.bid and c.ask and c.bid > 0 and (c.ask + c.bid) > 0
    ]
    median_spread_pct: Any = statistics.median(spreads) if spreads else NO_DATA
    total_oi = sum(c.open_interest or 0 for c in contracts)

    return {
        "iv_atm": iv_atm,
        "put_call_skew": put_call_skew,
        "term_structure": term_structure,
        "median_spread_pct": median_spread_pct,
        "total_open_interest": total_oi,
    }


def _spot_estimate(snapshot: Any, bars: Any) -> float | None:
    if isinstance(snapshot, dict):
        trade = snapshot.get("latestTrade")
        if isinstance(trade, dict):
            price = finite_float(trade.get("p"))
            if price is not None:
                return price
        quote = snapshot.get("latestQuote")
        if isinstance(quote, dict):
            bid, ask = finite_float(quote.get("bp")), finite_float(quote.get("ap"))
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                return (bid + ask) / 2
    if isinstance(bars, list) and bars:
        last_close = finite_float(bars[-1].get("c"))
        if last_close is not None:
            return last_close
    return None


async def _iv_rank(store: Store, symbol: str, iv_atm: Any) -> Any:
    if iv_atm == NO_DATA:
        return NO_DATA
    history = await store.recent_iv(symbol, n=90)
    past = [v for row in history if (v := finite_float(row.get("iv_atm"))) is not None]
    if not past:
        # No history yet: today's reading is, by definition, the only one —
        # a real computed value, not an unavailable one.
        return 0.0
    below_or_equal = sum(1 for value in past if value <= iv_atm)
    return below_or_equal / len(past)


async def collect(
    symbol: str, *, mcp: AlpacaMcp, store: Store, settings: Settings
) -> dict[str, Any]:
    """Deterministic evidence pack for ``symbol``. Persists IV history as a side effect."""
    snapshot = await _safe("snapshot", symbol, mcp.get_stock_snapshot(symbol))
    bars = await _safe("bars", symbol, mcp.get_stock_bars(symbol, limit=60))
    indicators = _indicators(bars) if isinstance(bars, list) else {
        "sma20": NO_DATA,
        "sma50": NO_DATA,
        "rsi14": NO_DATA,
        "atr14": NO_DATA,
        "realized_vol_20d": NO_DATA,
        "pct_from_period_high": NO_DATA,
        "pct_from_period_low": NO_DATA,
    }

    today = date.today()
    gte = today.isoformat()
    lte = (today + timedelta(days=settings.risk_dte_max)).isoformat()
    contracts = await _safe(
        "option_contracts",
        symbol,
        mcp.get_option_contracts(symbol, expiration_date_gte=gte, expiration_date_lte=lte),
    )
    snapshots = await _safe(
        "option_chain",
        symbol,
        mcp.get_option_chain(symbol, expiration_date_gte=gte, expiration_date_lte=lte),
    )
    spot = _spot_estimate(snapshot, bars)
    options = _options_summary(contracts, snapshots, spot)
    await store.save_iv_history(
        symbol=symbol,
        iv_atm=options["iv_atm"] if options["iv_atm"] != NO_DATA else None,
        put_call_skew=options["put_call_skew"] if options["put_call_skew"] != NO_DATA else None,
        term_structure=options["term_structure"] if options["term_structure"] != NO_DATA else None,
        median_spread_pct=(
            options["median_spread_pct"] if options["median_spread_pct"] != NO_DATA else None
        ),
        total_open_interest=(
            options["total_open_interest"] if options["total_open_interest"] != NO_DATA else None
        ),
    )
    options["iv_rank"] = await _iv_rank(store, symbol, options["iv_atm"])

    news_raw = await _safe("news", symbol, mcp.get_news([symbol], limit=5))
    untrusted_news: list[dict[str, str]] = []
    if isinstance(news_raw, list):
        for item in news_raw:
            if not isinstance(item, dict):
                continue
            headline = str(item.get("headline", ""))[:_NEWS_TRUNCATE]
            summary = str(item.get("summary", ""))[:_NEWS_TRUNCATE]
            untrusted_news.append({"headline": headline, "summary": summary})

    position = await _safe("position", symbol, mcp.get_open_position(symbol))

    return {
        "symbol": symbol,
        "note": NOTE,
        "snapshot": snapshot,
        "indicators": indicators,
        "options": options,
        "untrusted_news": untrusted_news,
        "lessons": await store.recent_lessons(symbol, 5),
        "position": position,
    }
