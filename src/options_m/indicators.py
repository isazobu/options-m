"""Price-series technical indicators, computed from raw bars.

Pure standard library, matching :mod:`options_m.volatility`. The evidence pack
needs SMA/RSI/ATR/realised-vol/52-week context for one underlying, and every one
of them is a few lines of arithmetic — not a reason to take on ``pandas`` or
``stockstats``.

Conventions:

* Inputs are plain sequences: closing prices as ``Sequence[float]``, or OHLC
  bars as mappings carrying ``"h"``, ``"l"`` and ``"c"`` (the Alpaca bar keys).
* Every function returns ``None`` rather than raising when there is not enough
  data or the data is unusable. A partial history yields a partial pack, never a
  fabricated number.
* RSI and ATR use Wilder's smoothing, the definition every charting package
  ships by default.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence

TRADING_DAYS_PER_YEAR = 252


def _finite(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _closes(values: Sequence[float | None]) -> list[float]:
    return [v for v in (_finite(x) for x in values) if v is not None]


def _hlc(bar: Mapping[str, object]) -> tuple[float, float, float] | None:
    high, low, close = _finite(bar.get("h")), _finite(bar.get("l")), _finite(bar.get("c"))
    if high is None or low is None or close is None:
        return None
    return high, low, close


def sma(values: Sequence[float | None], window: int) -> float | None:
    """Simple moving average of the last ``window`` finite values."""
    xs = _closes(values)
    if window <= 0 or len(xs) < window:
        return None
    return sum(xs[-window:]) / window


def rsi(values: Sequence[float | None], period: int = 14) -> float | None:
    """Wilder's Relative Strength Index over ``period`` (0..100)."""
    xs = _closes(values)
    if period <= 0 or len(xs) < period + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for previous, current in itertools.pairwise(xs):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def atr(bars: Sequence[Mapping[str, object]], period: int = 14) -> float | None:
    """Wilder's Average True Range over ``period``, in price units."""
    rows = [row for row in (_hlc(bar) for bar in bars) if row is not None]
    if period <= 0 or len(rows) < period + 1:
        return None

    true_ranges: list[float] = []
    for (_, _, prev_close), (high, low, _) in itertools.pairwise(rows):
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    value = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        value = (value * (period - 1) + true_range) / period
    return value


def realised_volatility(
    values: Sequence[float | None],
    window: int = 20,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float | None:
    """Annualised close-to-close realised volatility over ``window`` returns.

    A vol fraction (``0.24`` == 24%), the same units as an implied vol, so the
    two are directly comparable in the evidence pack.
    """
    xs = [v for v in _closes(values) if v > 0.0]
    if window <= 1 or len(xs) < window + 1:
        return None

    tail = xs[-(window + 1) :]
    # tail[1:] is one shorter by construction — pairwise, not strict.
    returns = [math.log(b / a) for a, b in itertools.pairwise(tail)]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(periods_per_year)


def distance_from_high_pct(
    values: Sequence[float | None], current: float | None = None
) -> float | None:
    """Percent the current level sits below the window high (``<= 0``)."""
    xs = _closes(values)
    if not xs:
        return None
    cur = xs[-1] if current is None else current
    high = max(xs)
    if high <= 0.0:
        return None
    return (cur - high) / high * 100.0


def distance_from_low_pct(
    values: Sequence[float | None], current: float | None = None
) -> float | None:
    """Percent the current level sits above the window low (``>= 0``)."""
    xs = _closes(values)
    if not xs:
        return None
    cur = xs[-1] if current is None else current
    low = min(xs)
    if low <= 0.0:
        return None
    return (cur - low) / low * 100.0


def window_extremes(values: Sequence[float | None]) -> tuple[float | None, float | None]:
    """``(low, high)`` of the finite values, or ``(None, None)`` when empty."""
    xs = _closes(values)
    if not xs:
        return None, None
    return min(xs), max(xs)
