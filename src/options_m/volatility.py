"""Option volatility math: Black-Scholes-Merton and IV Rank.

Pure standard library — no NumPy, no SciPy, no broker SDK. Kept deliberately
small: the evidence pack (phase 2) needs a delta for contract selection and an
IV Rank for the volatility analyst, and both are a page of arithmetic.

Conventions, matching the rest of the codebase:

* ``t`` is time to expiry in **years**, ``r`` a continuous risk-free rate, ``q``
  a continuous dividend yield, ``sigma`` an annualised vol as a fraction
  (``0.25`` == 25%).
* ``implied_vol`` returns ``None`` rather than raising when no finite vol
  reproduces the price — a routine outcome with real quotes (stale prints, a
  deep-ITM contract at intrinsic). Callers decide what an absent IV means.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

OptionType = Literal["call", "put"]
CALL: OptionType = "call"
PUT: OptionType = "put"

_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _d1_d2(S: float, K: float, t: float, r: float, q: float, sigma: float) -> tuple[float, float]:
    sqrt_t = math.sqrt(t)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    return d1, d1 - sigma * sqrt_t


def bsm_price(
    S: float, K: float, t: float, r: float, q: float, sigma: float, kind: OptionType
) -> float:
    """European option price under BSM with a continuous dividend yield."""
    if t <= 0.0 or sigma <= 0.0:
        # Degenerate: discounted intrinsic on the forward.
        fwd = S * math.exp((r - q) * max(t, 0.0))
        intrinsic = max(fwd - K, 0.0) if kind == CALL else max(K - fwd, 0.0)
        return math.exp(-r * max(t, 0.0)) * intrinsic

    d1, d2 = _d1_d2(S, K, t, r, q, sigma)
    disc_q = math.exp(-q * t)
    disc_r = math.exp(-r * t)
    if kind == CALL:
        return S * disc_q * _norm_cdf(d1) - K * disc_r * _norm_cdf(d2)
    return K * disc_r * _norm_cdf(-d2) - S * disc_q * _norm_cdf(-d1)


def bsm_vega(S: float, K: float, t: float, r: float, q: float, sigma: float) -> float:
    """dPrice/dSigma, per 1.00 (100 percentage points) of vol. Same for call and put."""
    if t <= 0.0 or sigma <= 0.0:
        return 0.0
    d1, _ = _d1_d2(S, K, t, r, q, sigma)
    return S * math.exp(-q * t) * _norm_pdf(d1) * math.sqrt(t)


def no_arb_bounds(
    S: float, K: float, t: float, r: float, q: float, kind: OptionType
) -> tuple[float, float]:
    """``(lower, upper)`` no-arbitrage price bounds for the option."""
    disc_q = math.exp(-q * t)
    disc_r = math.exp(-r * t)
    if kind == CALL:
        return max(S * disc_q - K * disc_r, 0.0), S * disc_q
    return max(K * disc_r - S * disc_q, 0.0), K * disc_r


@dataclass(frozen=True)
class Greeks:
    """First-order sensitivities. ``theta`` is per year; ``vega`` per 1.00 of vol."""

    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float


def bsm_greeks(
    S: float, K: float, t: float, r: float, q: float, sigma: float, kind: OptionType
) -> Greeks:
    """Analytic BSM greeks. Used to delta-target a strike when the chain snapshot
    carries no greeks of its own."""
    if t <= 0.0 or sigma <= 0.0:
        # At/after expiry the option is its intrinsic value: delta is a step,
        # every other sensitivity is zero.
        delta = (1.0 if S > K else 0.0) if kind == CALL else -1.0 if S < K else 0.0
        return Greeks(delta=delta, gamma=0.0, vega=0.0, theta=0.0, rho=0.0)

    d1, d2 = _d1_d2(S, K, t, r, q, sigma)
    sqrt_t = math.sqrt(t)
    disc_q = math.exp(-q * t)
    disc_r = math.exp(-r * t)
    pdf_d1 = _norm_pdf(d1)

    gamma = disc_q * pdf_d1 / (S * sigma * sqrt_t)
    vega = S * disc_q * pdf_d1 * sqrt_t
    common_theta = -S * disc_q * pdf_d1 * sigma / (2.0 * sqrt_t)

    if kind == CALL:
        delta = disc_q * _norm_cdf(d1)
        theta = common_theta - r * K * disc_r * _norm_cdf(d2) + q * S * disc_q * _norm_cdf(d1)
        rho = K * t * disc_r * _norm_cdf(d2)
    else:
        delta = -disc_q * _norm_cdf(-d1)
        theta = common_theta + r * K * disc_r * _norm_cdf(-d2) - q * S * disc_q * _norm_cdf(-d1)
        rho = -K * t * disc_r * _norm_cdf(-d2)

    return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho)


def implied_vol(
    price: float | None,
    S: float,
    K: float,
    t: float,
    r: float,
    q: float,
    kind: OptionType,
    *,
    lo: float = 1e-4,
    hi: float = 5.0,
    tol: float = 1e-7,
    max_iter: int = 100,
) -> float | None:
    """Recover sigma from an observed option price.

    Returns ``None`` when the price is missing, non-positive, or outside the
    no-arbitrage band. Uses a Newton step safeguarded by bisection on a verified
    bracket.
    """
    if price is None or price <= 0.0 or S <= 0.0 or K <= 0.0 or t <= 0.0:
        return None

    lower, upper = no_arb_bounds(S, K, t, r, q, kind)
    # A hair of slack so a price just above intrinsic still solves, to a tiny vol.
    if price <= lower + 1e-10 or price >= upper - 1e-12:
        return None

    def f(sigma: float) -> float:
        return bsm_price(S, K, t, r, q, sigma, kind) - price

    a, b = lo, hi
    if f(a) > 0.0:
        return None  # price below the value at ~zero vol; unsolvable
    if f(b) < 0.0:
        b = 10.0
        if f(b) < 0.0:
            return None  # even at 1000% vol the model price is too low

    sigma = 0.5 if a < 0.5 < b else 0.5 * (a + b)
    for _ in range(max_iter):
        diff = f(sigma)
        if abs(diff) < tol:
            return sigma
        if diff > 0.0:
            b = sigma
        else:
            a = sigma
        v = bsm_vega(S, K, t, r, q, sigma)
        nxt = sigma - diff / v if v > 1e-12 else 0.5 * (a + b)
        if not (a < nxt < b):
            nxt = 0.5 * (a + b)
        if abs(nxt - sigma) < tol:
            return nxt
        sigma = nxt
    return sigma


def iv_rank(values: Sequence[float | None], current: float | None = None) -> float | None:
    """IV Rank: where ``current`` sits between the window's min and max, 0..100.

    ``(current - min) / (max - min) * 100`` — the common tastytrade definition.
    ``current`` defaults to the last non-null value. Needs at least two
    observations; a flat window is defined as 0.
    """
    xs = [v for v in values if v is not None]
    if len(xs) < 2:
        return None
    cur = xs[-1] if current is None else current
    lo, hi = min(xs), max(xs)
    if hi == lo:
        return 0.0
    return (cur - lo) / (hi - lo) * 100.0


def iv_percentile(values: Sequence[float | None], current: float | None = None) -> float | None:
    """Share of the window strictly below ``current``, 0..100."""
    xs = [v for v in values if v is not None]
    if not xs:
        return None
    cur = xs[-1] if current is None else current
    return sum(1 for v in xs if v < cur) / len(xs) * 100.0
