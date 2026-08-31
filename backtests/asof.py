"""An as-of replacement for ``AlpacaMcp``, backed by historical Alpaca data.

The point of this class is that *nothing downstream knows it exists*. It
implements the six read methods ``EvidenceCollector`` and ``fetch_chain_window``
actually call, so the real ``EvidenceCollector``, ``matrix.decide``,
``strategy_builder.build`` and ``RiskEngine.evaluate`` run unmodified against a
past date. A backtest that reimplemented the pipeline would be measuring the
reimplementation.

What the historical data cannot give us, and how that is handled
----------------------------------------------------------------
Alpaca publishes historical option **bars** and **trades**, but there is no
historical option **quote** endpoint — only ``latest``. So a past bid/ask cannot
be retrieved, it has to be modelled:

* ``mid`` is the daily bar close for that contract on that date;
* ``bid``/``ask`` are placed symmetrically around it at ``spread_pct``, snapped
  to the real tick grid ($0.01 under $3.00, $0.05 above).

Two consequences to keep in view when reading any result:

1. ``MAX_SPREAD_PCT`` is a *modelled* gate here, not an observed one. Whether a
   proposal passes it is a function of the assumption, so the driver sweeps
   ``spread_pct`` rather than reporting a single number as fact.
2. ``impliedVolatility`` and ``greeks`` are served as ``None`` — which is what a
   paper account's feed does anyway — so IV and delta come from the project's
   own Black-Scholes solve on the modelled mid. That is the same code path the
   live service takes, not a backtest-only shortcut.

A contract with no bar on a given date is absent from that date's chain. No
trades printed means nothing to price it from, which is a reasonable stand-in
for "not tradeable that day", but it does bias the selectable set toward liquid
strikes. ``open_interest`` is passed through from Alpaca's contracts endpoint, which
carries it for roughly 70% of the contracts in this band. It is **not** a
point-in-time series: every row is stamped with a single ``open_interest_date``
(the most recent one Alpaca holds), so a decision replayed on 24 August is
gated on OI measured a few days later. Open interest moves slowly enough that
this is a small distortion, but it is look-ahead and is recorded as such in the
run's ``notes.md``. A contract with no OI at all is served as ``None``, which
``RiskEngine`` treats as unknown and refuses — the conservative direction.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

_PENNY_TICK_BELOW = 3.00


def _snap_to_tick(price: float) -> float:
    """Round to the tick the OCC actually quotes on."""
    tick = 0.01 if price < _PENNY_TICK_BELOW else 0.05
    return round(round(price / tick) * tick, 2)


class AsOfMcp:
    """Serves one replay date's view of the market from cached historical data."""

    def __init__(
        self,
        raw_dir: Path,
        *,
        as_of: date,
        spread_pct: float = 0.02,
    ) -> None:
        self._as_of = as_of
        self._spread_pct = spread_pct
        self._stock_bars: dict[str, list[dict[str, Any]]] = json.loads(
            (raw_dir / "bars_universe.json").read_text()
        )["bars"]
        self._contracts: dict[str, list[dict[str, Any]]] = json.loads(
            (raw_dir / "contracts.json").read_text()
        )
        option_bars: dict[str, list[dict[str, Any]]] = json.loads(
            (raw_dir / "option_bars.json").read_text()
        )
        stamp = as_of.isoformat()
        # Collapse to the one bar per contract that belongs to the replay date.
        self._option_bar_on_date = {
            occ: bar
            for occ, bars in option_bars.items()
            for bar in bars
            if bar["t"][:10] == stamp
        }
        self._contract_by_symbol = {
            row["symbol"]: row for rows in self._contracts.values() for row in rows
        }

    # --- introspection the harness reports on, not part of the MCP surface ---

    @property
    def priced_contracts(self) -> int:
        return len(self._option_bar_on_date)

    # --- the AlpacaMcp read surface the pipeline calls -----------------------

    async def get_stock_snapshot(self, symbol: str) -> dict[str, Any]:
        bar = self._stock_bar_on_date(symbol)
        if bar is None:
            msg = f"no {symbol} bar on {self._as_of}"
            raise RuntimeError(msg)
        close = float(bar["c"])
        return {
            "latestTrade": {"p": close, "t": f"{self._as_of.isoformat()}T20:00:00Z"},
            "latestQuote": {"bp": close, "ap": close},
            "dailyBar": bar,
        }

    async def get_stock_bars(
        self, symbol: str, *, timeframe: str = "1Day", limit: int = 252
    ) -> list[dict[str, Any]]:
        """Bars up to and including the replay date — never past it.

        This is the single most important line in the file: sliced the other way
        it would hand the trend block bars from the future and every SMA, RSI and
        realised-vol figure downstream would be look-ahead."""
        stamp = self._as_of.isoformat()
        history = [bar for bar in self._stock_bars.get(symbol, []) if bar["t"][:10] <= stamp]
        return history[-limit:]

    async def get_option_chain(
        self,
        underlying: str,
        *,
        option_type: str | None = None,
        expiration_gte: str | None = None,
        expiration_lte: str | None = None,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        limit: int = 1000,
        max_pages: int = 25,
        feed: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        snapshots: dict[str, dict[str, Any]] = {}
        for row in self._matching_contracts(
            underlying, option_type, expiration_gte, expiration_lte, strike_gte, strike_lte
        ):
            bar = self._option_bar_on_date.get(row["symbol"])
            if bar is None:
                continue
            mid = float(bar["c"])
            if mid <= 0:
                continue
            half = mid * self._spread_pct / 2
            bid, ask = _snap_to_tick(mid - half), _snap_to_tick(mid + half)
            if bid <= 0:
                bid = 0.01
            snapshots[row["symbol"]] = {
                "latestQuote": {"bp": bid, "ap": ask, "bs": 10, "as": 10},
                "latestTrade": {"p": mid, "s": int(bar.get("v") or 0)},
                # Served as None on purpose: the paper feed does the same, so the
                # project's own BS solve runs, exactly as it does live.
                "impliedVolatility": None,
                "greeks": None,
                "dailyBar": bar,
            }
        return snapshots

    async def get_option_contracts(
        self,
        underlying: str,
        *,
        option_type: str | None = None,
        expiration_gte: str | None = None,
        expiration_lte: str | None = None,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        limit: int = 1000,
        max_pages: int = 25,
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self._matching_contracts(
                underlying, option_type, expiration_gte, expiration_lte, strike_gte, strike_lte
            )
            if row["symbol"] in self._option_bar_on_date
        ]

    async def get_all_positions(self) -> list[dict[str, Any]]:
        """The replay opens positions but never carries them between symbols;
        the driver tracks the portfolio itself and passes it to the risk gate."""
        return []

    async def get_news(self, symbols: Any, limit: int = 20) -> list[dict[str, Any]]:
        """Alpaca's news endpoint is not point-in-time queryable per symbol here,
        and the matrix never reads news — only the LLM does, and the LLM is
        stubbed in this run. Serving nothing is honest; serving today's news
        would be look-ahead."""
        return []

    # --- helpers -------------------------------------------------------------

    def _stock_bar_on_date(self, symbol: str) -> dict[str, Any] | None:
        stamp = self._as_of.isoformat()
        for bar in reversed(self._stock_bars.get(symbol, [])):
            if bar["t"][:10] == stamp:
                return bar
        return None

    def _matching_contracts(
        self,
        underlying: str,
        option_type: str | None,
        expiration_gte: str | None,
        expiration_lte: str | None,
        strike_gte: float | None,
        strike_lte: float | None,
    ) -> list[dict[str, Any]]:
        rows = self._contracts.get(underlying.upper(), [])
        out = []
        for row in rows:
            if option_type is not None and row["type"] != option_type:
                continue
            expiry = row["expiration_date"]
            if expiration_gte is not None and expiry < expiration_gte:
                continue
            if expiration_lte is not None and expiry > expiration_lte:
                continue
            strike = float(row["strike_price"])
            if strike_gte is not None and strike < float(strike_gte):
                continue
            if strike_lte is not None and strike > float(strike_lte):
                continue
            out.append(row)
        return out


class StubStore:
    """The Store surface ``EvidenceCollector`` touches, with no database.

    Every call the collector makes here is already wrapped in ``try/except`` on
    its side and degrades to ``NO_DATA_AVAILABLE``. IV rank and percentile come
    back ``None``, which costs the decision nothing: ``matrix.decide`` reads
    ``iv_atm`` and ``realised_vol_20d`` and never looks at either rank.
    """

    def __init__(self) -> None:
        self.iv_history: dict[str, list[dict[str, Any]]] = {}

    async def append_iv_snapshot(
        self,
        symbol: str,
        *,
        iv_atm: float,
        dte: int | None = None,
        spot: float | None = None,
        payload: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        self.iv_history.setdefault(symbol, []).append({"iv_atm": iv_atm, "dte": dte, "ts": ts})

    async def iv_rank_and_percentile(
        self, symbol: str, *, days: int = 252, min_days: int = 126
    ) -> tuple[float | None, float | None, int]:
        # A replay has no stored IV history to rank against, so the honest
        # answer is the same one the real store gives on a cold database.
        return None, None, 0

    async def iv_rank_for(self, symbol: str, *, days: int = 252, min_days: int = 126) -> None:
        return None

    async def recent_iv(self, symbol: str, limit: int = 60) -> list[dict[str, Any]]:
        return list(reversed(self.iv_history.get(symbol, [])))

    async def recent_lessons(self, symbol: str | None, limit: int) -> list[str]:
        return []
