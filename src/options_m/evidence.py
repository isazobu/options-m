
"""Deterministic evidence collection for one underlying.

This is the data-gathering step of the agent chain. It calls Alpaca (reads
only), turns the raw payloads into a small, self-describing dict, and hands that
dict to the reasoning layer — the LLM crew in phase 3, and ultimately whatever
judge replays the decision from ``proposals.evidence``.

Two rules shape everything here:

* **No LLM, no randomness.** Every number in the pack is either measured or
  computed by arithmetic in this process. The reasoning happens later, on top of
  this; the pack itself must be reproducible from the same market state.
* **Missing means missing.** A field we could not fetch is the literal string
  ``"NO_DATA_AVAILABLE"`` — never ``0``, ``None`` dressed up as a value, or an
  estimate. The pack carries a top-level ``note`` saying so, so the model has no
  excuse to invent an IV or a spread. (Borrowed from TradingAgents'
  ``dataflows/interface.py``.)

The pack targets a few KB of JSON: summaries, not raw chains.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any

from options_m.config import Settings
from options_m.indicators import (
    atr,
    distance_from_high_pct,
    distance_from_low_pct,
    realised_volatility,
    rsi,
    sma,
    window_extremes,
)
from options_m.mcp_client import AlpacaMcp, finite_float
from options_m.occ import parse_occ_symbol
from options_m.store import Store
from options_m.volatility import implied_vol, iv_percentile

logger = logging.getLogger(__name__)

# The exact wording TradingAgents uses; it is what stops a model treating an
# absent field as licence to guess.
MISSING = "NO_DATA_AVAILABLE"
NOTE = (
    f"Fields marked {MISSING} are genuinely unavailable. "
    "Do not estimate, infer, or fabricate values for them."
)

DEFAULT_DTE_MIN = 7
DEFAULT_DTE_MAX = 45
# Daily bars pulled for the trend block. Enough for a real 52-week range and for
# SMA50 to be defined from the tail.
BARS_LOOKBACK = 252
# Strike band around spot for the chain/contract pulls, as a fraction of spot.
STRIKE_BAND = 0.15
CHAIN_LIMIT = 250
NEWS_LIMIT = 5
NEWS_HEADLINE_CHARS = 200
NEWS_SUMMARY_CHARS = 320
# Flat risk-free / dividend assumptions for the BSM IV fallback. The pack notes
# when a value came from this path rather than the chain's own IV.
_RISK_FREE = 0.0
_DIV_YIELD = 0.0


def _f(value: object) -> float | None:
    return finite_float(value)


def _i(value: object) -> int | None:
    number = finite_float(value)
    return int(number) if number is not None else None


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _or_missing(value: float | int | str | None) -> float | int | str:
    return value if value is not None else MISSING


def _truncate(text: object, limit: int) -> str | None:
    if not isinstance(text, str):
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class EvidenceCollector:
    """Assembles the per-underlying evidence pack from Alpaca reads."""

    def __init__(self, settings: Settings, mcp: AlpacaMcp, store: Store) -> None:
        self._settings = settings
        self._mcp = mcp
        self._store = store

    async def collect(
        self,
        symbol: str,
        *,
        dte_min: int = DEFAULT_DTE_MIN,
        dte_max: int = DEFAULT_DTE_MAX,
    ) -> dict[str, Any]:
        """Build the evidence pack for ``symbol``.

        Never raises for a single failed sub-fetch — that section degrades to
        ``NO_DATA_AVAILABLE`` and the rest of the pack is still returned.
        """
        symbol = symbol.upper()
        now = datetime.now(UTC)

        spot_block, spot_price = await self._spot(symbol)
        trend_block = await self._trend(symbol, spot_price)
        realised_vol = (
            trend_block.get("realised_vol_20d") if isinstance(trend_block, dict) else None
        )
        if not isinstance(realised_vol, float):
            realised_vol = None
        options_block = await self._options(
            symbol, spot_price, dte_min, dte_max, now, realised_vol
        )

        return {
            "symbol": symbol,
            "as_of": now.isoformat().replace("+00:00", "Z"),
            "note": NOTE,
            "dte_window": [dte_min, dte_max],
            "spot": spot_block,
            "trend": trend_block,
            "options": options_block,
            "position": await self._position(symbol),
            "untrusted_news": await self._news(symbol),
            "lessons": await self._lessons(symbol),
        }

    # ---- sections ---------------------------------------------------------

    async def _spot(self, symbol: str) -> tuple[dict[str, Any] | str, float | None]:
        """Underlying price picture from the stock snapshot.

        Returns the block and a best-guess spot price for the option maths
        (last trade, else daily close, else quote mid).
        """
        try:
            snap = await self._mcp.get_stock_snapshot(symbol)
        except Exception:
            logger.warning("evidence: stock snapshot failed for %s", symbol, exc_info=True)
            return MISSING, None

        quote = snap.get("latestQuote") or {}
        trade = snap.get("latestTrade") or {}
        day = snap.get("dailyBar") or {}
        prev = snap.get("prevDailyBar") or {}

        bid, ask = _f(quote.get("bp")), _f(quote.get("ap"))
        mid = (bid + ask) / 2 if bid and ask else None
        last = _f(trade.get("p"))
        day_close = _f(day.get("c"))
        prev_close = _f(prev.get("c"))
        spot = last or day_close or mid

        change_pct: float | None = None
        if spot is not None and prev_close:
            change_pct = (spot - prev_close) / prev_close * 100

        block: dict[str, Any] = {
            "bid": _or_missing(bid),
            "ask": _or_missing(ask),
            "bid_size": _or_missing(_i(quote.get("bs"))),
            "ask_size": _or_missing(_i(quote.get("as"))),
            "mid": _or_missing(_round(mid, 4)),
            "spread": _or_missing(_round(ask - bid, 4) if bid and ask else None),
            "spread_pct": _or_missing(
                _round((ask - bid) / mid * 100, 3) if mid and bid and ask else None
            ),
            "last": _or_missing(last),
            "day_open": _or_missing(_f(day.get("o"))),
            "day_high": _or_missing(_f(day.get("h"))),
            "day_low": _or_missing(_f(day.get("l"))),
            "day_close": _or_missing(day_close),
            "day_volume": _or_missing(_i(day.get("v"))),
            "day_vwap": _or_missing(_f(day.get("vw"))),
            "prev_close": _or_missing(prev_close),
            "change_from_prev_close_pct": _or_missing(_round(change_pct, 3)),
            "quote_time": quote.get("t") or MISSING,
        }
        return block, spot

    async def _trend(self, symbol: str, spot_price: float | None) -> dict[str, Any] | str:
        """Moving averages, RSI, ATR, realised vol and 52-week context, all
        computed here from daily bars."""
        try:
            bars = await self._mcp.get_stock_bars(symbol, timeframe="1Day", limit=BARS_LOOKBACK)
        except Exception:
            logger.warning("evidence: stock bars failed for %s", symbol, exc_info=True)
            return MISSING

        closes = [_f(bar.get("c")) for bar in bars]
        finite_closes = [c for c in closes if c is not None]
        if len(finite_closes) < 2:
            return MISSING

        reference = spot_price if spot_price is not None else finite_closes[-1]
        low_52w, high_52w = window_extremes(closes)
        atr14 = atr(bars, 14)

        return {
            "bars_used": len(finite_closes),
            "sma_20": _or_missing(_round(sma(closes, 20), 4)),
            "sma_50": _or_missing(_round(sma(closes, 50), 4)),
            "rsi_14": _or_missing(_round(rsi(closes, 14), 2)),
            "atr_14": _or_missing(_round(atr14, 4)),
            "atr_14_pct_of_spot": _or_missing(
                _round(atr14 / reference * 100, 3) if atr14 and reference else None
            ),
            "realised_vol_20d": _or_missing(_round(realised_volatility(closes, 20), 4)),
            "high_52w": _or_missing(_round(high_52w, 4)),
            "low_52w": _or_missing(_round(low_52w, 4)),
            "pct_from_52w_high": _or_missing(
                _round(distance_from_high_pct(closes, reference), 2)
            ),
            "pct_from_52w_low": _or_missing(_round(distance_from_low_pct(closes, reference), 2)),
        }

    async def _options(
        self,
        symbol: str,
        spot_price: float | None,
        dte_min: int,
        dte_max: int,
        now: datetime,
        realised_vol: float | None = None,
    ) -> dict[str, Any] | str:
        """Chain summary: ATM IV, IV rank/percentile, skew, term structure,
        median spread, total open interest, the two ATM contracts, and — since
        ``realised_vol`` (20-day, from the trend block) is passed in — the
        IV-minus-RV vol risk premium a judge reads as "options rich vs cheap"."""
        today = now.date()
        exp_gte = (today + timedelta(days=dte_min)).isoformat()
        exp_lte = (today + timedelta(days=dte_max)).isoformat()
        strike_gte: float | None = None
        strike_lte: float | None = None
        if spot_price:
            strike_gte = round(spot_price * (1 - STRIKE_BAND), 2)
            strike_lte = round(spot_price * (1 + STRIKE_BAND), 2)

        try:
            snapshots = await self._mcp.get_option_chain(
                symbol,
                expiration_gte=exp_gte,
                expiration_lte=exp_lte,
                strike_gte=strike_gte,
                strike_lte=strike_lte,
                limit=CHAIN_LIMIT,
            )
        except Exception:
            logger.warning("evidence: option chain failed for %s", symbol, exc_info=True)
            return MISSING
        if not snapshots:
            return MISSING

        rows = self._chain_rows(snapshots, spot_price, today)
        if not rows:
            return MISSING

        await self._attach_open_interest(symbol, rows, exp_gte, exp_lte, strike_gte, strike_lte)

        near_dte = min(row["dte"] for row in rows)
        far_dte = max(row["dte"] for row in rows)
        near_rows = [row for row in rows if row["dte"] == near_dte]
        far_rows = [row for row in rows if row["dte"] == far_dte]

        atm_call, iv_call, src_call = self._atm(near_rows, "call", spot_price)
        atm_put, iv_put, src_put = self._atm(near_rows, "put", spot_price)
        iv_atm_near = _mean(iv_call, iv_put)
        iv_atm_far = _mean(
            self._atm(far_rows, "call", spot_price)[1],
            self._atm(far_rows, "put", spot_price)[1],
        )
        iv_atm = iv_atm_near

        iv_rank, iv_pctile = await self._persist_and_rank(symbol, iv_atm, near_dte, spot_price)

        spreads = [row["spread_pct"] for row in rows if isinstance(row["spread_pct"], float)]
        ois = [row["open_interest"] for row in rows if isinstance(row["open_interest"], int)]

        iv_source = "chain" if "chain" in (src_call, src_put) else (src_call or src_put or MISSING)

        return {
            "dte_window": [dte_min, dte_max],
            "contracts_scanned": len(rows),
            "expiries_scanned": sorted({row["expiry"] for row in rows}),
            "near_expiry": near_rows[0]["expiry"],
            "near_dte": near_dte,
            "far_expiry": far_rows[0]["expiry"],
            "far_dte": far_dte,
            "iv_atm": _or_missing(_round(iv_atm, 4)),
            "iv_atm_near": _or_missing(_round(iv_atm_near, 4)),
            "iv_atm_far": _or_missing(_round(iv_atm_far, 4)),
            "iv_source": iv_source,
            "iv_rank": _or_missing(_round(iv_rank, 2)),
            "iv_percentile": _or_missing(_round(iv_pctile, 2)),
            "realised_vol_20d": _or_missing(_round(realised_vol, 4)),
            "iv_minus_rv": _or_missing(
                _round(iv_atm - realised_vol, 4)
                if iv_atm is not None and realised_vol is not None
                else None
            ),
            "put_call_skew": _or_missing(
                _round(iv_put - iv_call, 4) if iv_put is not None and iv_call is not None else None
            ),
            "term_structure": _or_missing(
                _round(iv_atm_far - iv_atm_near, 4)
                if iv_atm_far is not None and iv_atm_near is not None
                else None
            ),
            "median_spread_pct": _or_missing(
                _round(float(median(spreads)), 3) if spreads else None
            ),
            "total_open_interest": _or_missing(sum(ois) if ois else None),
            "atm_call": atm_call or MISSING,
            "atm_put": atm_put or MISSING,
        }

    def _chain_rows(
        self,
        snapshots: dict[str, dict[str, Any]],
        spot_price: float | None,
        today: date,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for occ_symbol, snap in snapshots.items():
            parsed = parse_occ_symbol(occ_symbol)
            if parsed is None:
                continue
            quote = snap.get("latestQuote") or {}
            greeks = snap.get("greeks") or {}
            bid, ask = _f(quote.get("bp")), _f(quote.get("ap"))
            mid = (bid + ask) / 2 if bid and ask and (bid + ask) > 0 else None
            spread_pct = (ask - bid) / mid * 100 if mid and bid and ask else None
            dte = (parsed.expiry - today).days
            iv = _f(snap.get("impliedVolatility"))
            iv_source = "chain" if iv is not None else None
            if iv is None and mid is not None and spot_price and dte > 0:
                iv = implied_vol(
                    mid,
                    spot_price,
                    parsed.strike,
                    dte / 365.0,
                    _RISK_FREE,
                    _DIV_YIELD,
                    parsed.option_type,
                )
                iv_source = "bsm_from_mid" if iv is not None else None
            rows.append(
                {
                    "symbol": occ_symbol,
                    "type": parsed.option_type,
                    "strike": parsed.strike,
                    "expiry": parsed.expiry.isoformat(),
                    "dte": dte,
                    "bid": bid,
                    "ask": ask,
                    "mid": _round(mid, 4),
                    "spread_pct": _round(spread_pct, 3),
                    "iv": _round(iv, 4),
                    "iv_source": iv_source,
                    "delta": _round(_f(greeks.get("delta")), 4),
                    "gamma": _round(_f(greeks.get("gamma")), 5),
                    "theta": _round(_f(greeks.get("theta")), 5),
                    "vega": _round(_f(greeks.get("vega")), 5),
                    "open_interest": None,  # filled from get_option_contracts below
                    "last": _round(_f((snap.get("latestTrade") or {}).get("p")), 4),
                }
            )
        return rows

    async def _attach_open_interest(
        self,
        symbol: str,
        rows: list[dict[str, Any]],
        exp_gte: str,
        exp_lte: str,
        strike_gte: float | None,
        strike_lte: float | None,
    ) -> None:
        """Open interest lives in the trading-API contract list, not the
        market-data chain. A failure here just leaves ``open_interest`` missing."""
        try:
            contracts = await self._mcp.get_option_contracts(
                symbol,
                expiration_gte=exp_gte,
                expiration_lte=exp_lte,
                strike_gte=strike_gte,
                strike_lte=strike_lte,
                limit=CHAIN_LIMIT,
            )
        except Exception:
            logger.warning("evidence: option contracts failed for %s", symbol, exc_info=True)
            return
        oi_by_symbol = {
            str(contract.get("symbol")): _i(contract.get("open_interest"))
            for contract in contracts
            if contract.get("symbol")
        }
        for row in rows:
            if row["open_interest"] is None:
                row["open_interest"] = oi_by_symbol.get(row["symbol"])

    def _atm(
        self,
        rows: list[dict[str, Any]],
        option_type: str,
        spot_price: float | None,
    ) -> tuple[dict[str, Any] | None, float | None, str | None]:
        """Pick the row of ``option_type`` whose strike is nearest spot."""
        candidates = [row for row in rows if row["type"] == option_type]
        if not candidates:
            return None, None, None
        if spot_price is not None:
            pick = min(candidates, key=lambda row: abs(row["strike"] - spot_price))
        else:
            pick = min(candidates, key=lambda row: row["spread_pct"] or float("inf"))
        compact = {
            "symbol": pick["symbol"],
            "strike": pick["strike"],
            "expiry": pick["expiry"],
            "dte": pick["dte"],
            "bid": _or_missing(pick["bid"]),
            "ask": _or_missing(pick["ask"]),
            "mid": _or_missing(pick["mid"]),
            "spread_pct": _or_missing(pick["spread_pct"]),
            "iv": _or_missing(pick["iv"]),
            "delta": _or_missing(pick["delta"]),
            "gamma": _or_missing(pick["gamma"]),
            "theta": _or_missing(pick["theta"]),
            "vega": _or_missing(pick["vega"]),
            "open_interest": _or_missing(pick["open_interest"]),
        }
        return compact, (pick["iv"] if isinstance(pick["iv"], float) else None), pick["iv_source"]

    async def _persist_and_rank(
        self,
        symbol: str,
        iv_atm: float | None,
        near_dte: int,
        spot_price: float | None,
    ) -> tuple[float | None, float | None]:
        """Append today's ATM-IV reading, then rank it against the stored
        history. IV rank is meaningless on day one and fills in as the service
        runs — which is why the write happens here, once per pull."""
        if iv_atm is None:
            return None, None
        try:
            await self._store.append_iv_snapshot(
                symbol,
                iv_atm=iv_atm,
                dte=near_dte,
                spot=spot_price,
                payload={"iv_atm": iv_atm},
            )
            rank = await self._store.iv_rank_for(symbol)
            history = await self._store.recent_iv(symbol)
        except Exception:
            logger.warning("evidence: iv history I/O failed for %s", symbol, exc_info=True)
            return None, None
        values = [_f(row.get("iv_atm")) for row in reversed(history)]
        # Like IV rank, a percentile against a single reading is noise, not data.
        pctile = iv_percentile(values) if len([v for v in values if v is not None]) >= 2 else None
        return rank, pctile

    async def _position(self, symbol: str) -> list[dict[str, Any]] | str | None:
        """Any open position in this underlying — equity or option legs.

        ``None`` means "confirmed flat"; ``NO_DATA_AVAILABLE`` means the read
        failed. The distinction matters to a judge.
        """
        try:
            positions = await self._mcp.get_all_positions()
        except Exception:
            logger.warning("evidence: positions read failed for %s", symbol, exc_info=True)
            return MISSING

        mine: list[dict[str, Any]] = []
        for position in positions:
            raw_symbol = str(position.get("symbol", "")).upper()
            parsed = parse_occ_symbol(raw_symbol)
            if parsed is not None and parsed.underlying == symbol:
                mine.append(
                    {
                        "kind": "option",
                        "symbol": raw_symbol,
                        "option_type": parsed.option_type,
                        "strike": parsed.strike,
                        "expiry": parsed.expiry.isoformat(),
                        "side": position.get("side"),
                        "qty": _or_missing(_f(position.get("qty"))),
                        "avg_entry_price": _or_missing(_f(position.get("avg_entry_price"))),
                        "market_value": _or_missing(_f(position.get("market_value"))),
                        "unrealized_pl": _or_missing(_f(position.get("unrealized_pl"))),
                        "unrealized_plpc": _or_missing(_f(position.get("unrealized_plpc"))),
                    }
                )
            elif raw_symbol == symbol:
                mine.append(
                    {
                        "kind": "equity",
                        "symbol": raw_symbol,
                        "side": position.get("side"),
                        "qty": _or_missing(_f(position.get("qty"))),
                        "avg_entry_price": _or_missing(_f(position.get("avg_entry_price"))),
                        "market_value": _or_missing(_f(position.get("market_value"))),
                        "unrealized_pl": _or_missing(_f(position.get("unrealized_pl"))),
                        "unrealized_plpc": _or_missing(_f(position.get("unrealized_plpc"))),
                    }
                )
        return mine or None

    async def _news(self, symbol: str) -> list[dict[str, Any]] | str:
        """Recent headlines, headline + summary only, truncated.

        The MCP server classes ``get_news`` output as ``external_text`` — text we
        did not author and cannot vouch for. It goes under a clearly named
        ``untrusted_news`` key; phase 3 fences it inside the prompt.
        """
        try:
            raw = await self._mcp.get_news([symbol], limit=NEWS_LIMIT)
        except Exception:
            logger.warning("evidence: news read failed for %s", symbol, exc_info=True)
            return MISSING

        if isinstance(raw, dict):
            items = raw.get("news") or raw.get("data") or raw.get("results") or []
        elif isinstance(raw, list):
            items = raw
        else:
            items = []

        headlines: list[dict[str, Any]] = []
        for item in items[:NEWS_LIMIT]:
            if not isinstance(item, dict):
                continue
            headlines.append(
                {
                    "headline": _truncate(item.get("headline"), NEWS_HEADLINE_CHARS) or MISSING,
                    "summary": _truncate(item.get("summary"), NEWS_SUMMARY_CHARS) or MISSING,
                    "source": item.get("source") or MISSING,
                    "created_at": item.get("created_at") or MISSING,
                }
            )
        return headlines

    async def _lessons(self, symbol: str) -> list[str]:
        try:
            symbol_lessons = await self._store.recent_lessons(symbol, 3)
            portfolio_lessons = await self._store.recent_lessons(None, 2)
        except Exception:
            logger.warning("evidence: lessons read failed for %s", symbol, exc_info=True)
            return []
        return [*symbol_lessons, *portfolio_lessons]


def _mean(*values: float | None) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None
