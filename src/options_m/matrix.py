"""Deterministic Strategy Matrix + earnings gate.

Pure code — zero LLM, zero MCP, zero imports from the reasoning layer.
``decide()`` receives the pre-computed evidence pack (written by
``MarketPulseAgent`` every 60s) and the LLM's ``RegimeRead`` (thesis +
conviction), and returns a ``StrategyIntent`` or the literal string ``"hold"``.

Step order is intentional:
  1. Earnings gate (cheapest check, short-circuits before any threshold math).
  2. Classify trend from SMA/RSI in the evidence pack.
  3. Classify IV regime from IV/RV ratio in the evidence pack.
  4. Matrix lookup → strategy family.
  5. Level degradation (account options-trading level < structure requirement).
  6. Conviction floor check (the one LLM-sourced veto).
  7. Assemble StrategyIntent.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from options_m.config import Settings
from options_m.earnings import is_earnings_blackout
from options_m.models import RegimeRead, StrategyIntent

# IV/RV thresholds for regime classification.
_IV_RV_EXPENSIVE = 1.10
_IV_RV_VERY_EXPENSIVE = 1.40

# RSI thresholds for trend classification.
_RSI_BULLISH = 55.0
_RSI_BEARISH = 45.0

# Strategy matrix: (trend, iv_regime) → strategy name.
_MATRIX: dict[tuple[str, str], str] = {
    ("up", "expensive"): "put_credit_spread",
    ("up", "very_expensive"): "put_credit_spread",
    ("up", "cheap"): "call_debit_spread",
    ("flat", "expensive"): "iron_condor",
    ("flat", "very_expensive"): "iron_butterfly",
    ("flat", "cheap"): "long_strangle",
    ("down", "expensive"): "call_credit_spread",
    ("down", "very_expensive"): "call_credit_spread",
    ("down", "cheap"): "put_debit_spread",
}

# Minimum options-trading level required for each structure.
_MIN_LEVEL: dict[str, int] = {
    "long_call": 2,
    "long_put": 2,
    "long_strangle": 2,
    "call_debit_spread": 3,
    "put_debit_spread": 3,
    "put_credit_spread": 3,
    "call_credit_spread": 3,
    "iron_condor": 3,
    "iron_butterfly": 3,
}

# Level-2 fallback for each Level-3 structure. None → "hold" (no safe downgrade).
_LEVEL2_FALLBACK: dict[str, str | None] = {
    "call_debit_spread": "long_call",
    "put_debit_spread": "long_put",
    "put_credit_spread": None,
    "call_credit_spread": None,
    "iron_condor": None,
    "iron_butterfly": None,
    "long_strangle": "long_strangle",  # already Level 2
}


def _trend_direction(trend: dict[str, Any]) -> Literal["up", "flat", "down"]:
    """Classify trend from SMA20/50 and RSI14 already in the evidence pack."""
    sma20 = trend.get("sma_20")
    sma50 = trend.get("sma_50")
    rsi14 = trend.get("rsi_14")
    if not isinstance(sma20, (int, float)) or not isinstance(sma50, (int, float)):
        return "flat"
    if not isinstance(rsi14, (int, float)):
        return "flat"
    if sma20 > sma50 and rsi14 > _RSI_BULLISH:
        return "up"
    if sma20 < sma50 and rsi14 < _RSI_BEARISH:
        return "down"
    return "flat"


def _iv_regime(options: dict[str, Any]) -> Literal["cheap", "expensive", "very_expensive"]:
    """Classify IV regime from ATM-IV and realised-vol already in the evidence pack.

    Falls back to "cheap" when data is missing — the conservative direction
    because it produces debit (defined-loss) structures rather than premium-
    selling structures into an opaque vol environment.
    """
    iv_atm = options.get("iv_atm")
    rv = options.get("realised_vol_20d")
    if not isinstance(iv_atm, (int, float)) or not isinstance(rv, (int, float)) or rv <= 0:
        return "cheap"
    ratio = iv_atm / rv
    if ratio >= _IV_RV_VERY_EXPENSIVE:
        return "very_expensive"
    if ratio >= _IV_RV_EXPENSIVE:
        return "expensive"
    return "cheap"


def decide(
    evidence: dict[str, Any],
    regime: RegimeRead,
    *,
    settings: Settings,
    as_of: date | None = None,
) -> StrategyIntent | Literal["hold"]:
    """Return a StrategyIntent or ``"hold"``.

    ``evidence`` is the full evidence pack from the local cache (written by
    MarketPulseAgent). ``regime`` is the LLM's thesis/invalidation/conviction.
    Both are required; neither is optional — a caller that cannot provide one
    of them should not call this function.
    """
    symbol = str(evidence.get("symbol", "")).upper()
    today = as_of or date.today()

    # 1. Earnings gate — check before the matrix so a blacked-out symbol never
    # reaches a threshold computation at all.
    if is_earnings_blackout(symbol, today):
        return "hold"

    # 2-3. Classify trend and IV regime from pre-computed indicators.
    trend_block = evidence.get("trend")
    options_block = evidence.get("options")
    if not isinstance(trend_block, dict) or not isinstance(options_block, dict):
        return "hold"

    trend = _trend_direction(trend_block)
    iv_regime_label = _iv_regime(options_block)

    # 4. Matrix lookup.
    strategy = _MATRIX.get((trend, iv_regime_label))
    if strategy is None:
        return "hold"

    # 5. Level degradation. Effective level = min(account level, config cap).
    cached_level = evidence.get("options_trading_level")
    effective_level = settings.options_level
    if isinstance(cached_level, int) and cached_level > 0:
        effective_level = min(cached_level, settings.options_level)

    required_level = _MIN_LEVEL.get(strategy, 3)
    if effective_level < required_level:
        fallback = _LEVEL2_FALLBACK.get(strategy)
        if fallback is None:
            return "hold"
        strategy = fallback

    # 6. Conviction floor — the one LLM-sourced veto.
    if regime.conviction < settings.conviction_floor:
        return "hold"

    # 7. Assemble StrategyIntent.
    spread_width: float | None = None
    if strategy not in {"long_call", "long_put", "long_strangle"}:
        spread_width = settings.spread_width_default

    return StrategyIntent(
        action="open",
        strategy=strategy,  # type: ignore[arg-type]
        underlying=symbol,
        target_delta=settings.short_delta_default,
        spread_width=spread_width,
        dte_min=settings.dte_target_min,
        dte_max=settings.dte_target_max,
        conviction=regime.conviction,
        thesis=regime.thesis,
        invalidation=regime.invalidation,
    )
