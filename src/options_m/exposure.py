"""Portfolio-level option exposure: beta-weighted delta and net vega.

Position-count and dollar-risk caps treat five positions as five independent
bets. In this universe they are not. SPY, QQQ, IWM and six large-cap tech names
correlate somewhere around 0.8-0.9, and the Strategy Matrix reaches for the same
structure family across all of them at once whenever the IV regime is the same —
so a book of five short-premium spreads is one short-vol, long-delta position
wearing five hats. ``max_concurrent_positions`` cannot see that; these two
measures can:

* **Beta-weighted dollar delta** — every position's directional exposure
  restated in index-equivalent dollars, so exposure that is correlated adds up
  instead of looking diversified.
* **Net vega** — dollars gained or lost per one point of implied volatility
  across the whole book. Five short-vol spreads have five times the vol
  exposure of one, whatever their individual max losses say.

Pure functions, no LLM, no MCP, no store writes — the same discipline as
:mod:`options_m.risk` and :mod:`options_m.sizing`, so a mistake in the agent
layer cannot reason its way to a bigger number.

Two documented simplifications, both in the conservative direction:

* One implied volatility per underlying (the evidence pack's ``iv_atm``) is used
  for every strike, rather than each strike's own vol. There is no smile here.
  Vega peaks at the money and the structures this system builds are near it, so
  the ATM vol is the right single number if there has to be one.
* Betas are hand-maintained (see :data:`_BETA`), the same idiom as
  :mod:`options_m.earnings`. An unlisted symbol is assumed *more* volatile than
  the index, never less, so an unknown name overstates its own exposure and
  sizes the book down.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict

from options_m.evidence.occ import parse_occ_symbol
from options_m.mcp_client import finite_float
from options_m.models import OrderPlan

_CONTRACT_MULTIPLIER = 100.0

# Rough betas to SPY for the configured universe. Hand-maintained on purpose:
# deriving them live would mean a returns regression per symbol per tick, and the
# number only has to be right to about a tenth for a risk cap to do its job.
# Update alongside `universe` in Settings.
_BETA = {
    "SPY": 1.00,
    "QQQ": 1.15,
    "IWM": 1.10,
    "AAPL": 1.15,
    "MSFT": 1.10,
    "NVDA": 1.90,
    "AMD": 2.00,
    "TSLA": 2.10,
    "META": 1.35,
    "GOOGL": 1.10,
}

# What an unlisted symbol is assumed to be. Deliberately above 1.0: overstating
# beta overstates the book's exposure, which sizes the next trade *down*. A
# default of 1.0 would let an unknown high-beta name in under the cap.
_DEFAULT_BETA = 1.50


def beta_for(symbol: str) -> float:
    """Beta to SPY for ``symbol``, or the conservative default."""
    return _BETA.get(symbol.strip().upper(), _DEFAULT_BETA)


def greeks_from_snapshot(snapshot: dict[str, Any]) -> tuple[float | None, float | None]:
    """``(delta, dollar_vega_per_contract)`` from an Alpaca option snapshot.

    Alpaca's ``greeks.vega`` is per share per one point of IV. The rest of
    this module stores dollars per contract per point, so the raw figure is
    multiplied by the contract size. Either field can be absent independently
    — the OPRA feed often has delta and drops vega on a short-dated weekly.
    """
    greeks = snapshot.get("greeks")
    if not isinstance(greeks, dict):
        return None, None
    delta = finite_float(greeks.get("delta"))
    raw_vega = finite_float(greeks.get("vega"))
    vega = None if raw_vega is None else raw_vega * _CONTRACT_MULTIPLIER
    return delta, vega


def bs_vega(
    *,
    spot: float,
    strike: float,
    dte_days: int,
    iv: float | None,
    risk_free_rate: float,
) -> float | None:
    """Black-Scholes vega per contract, per **one point** of implied vol.

    Calls and puts share a vega, so the option type is not a parameter. The raw
    formula gives the derivative per unit of vol (1.00 = 100 points), so the
    result is divided by 100 to read in the vol points every quote is stated in,
    and multiplied by the contract multiplier to be dollars per contract.

    ``None`` for any input that makes the formula undefined — never a fabricated
    number, the same contract as ``strategy_builder.bs_delta``.
    """
    if spot <= 0 or strike <= 0 or iv is None or iv <= 0 or dte_days <= 0:
        return None
    t = dte_days / 365.0
    try:
        d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    except (ValueError, ZeroDivisionError):
        return None
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return spot * pdf * math.sqrt(t) * _CONTRACT_MULTIPLIER / 100.0


class Exposure(BaseModel):
    """One book's, or one plan's, directional and volatility exposure.

    Both fields are nullable and mean "unknown", never zero: a leg whose greeks
    could not be computed makes the whole aggregate unknown rather than quietly
    contributing nothing, because contributing nothing is indistinguishable from
    a hedged position and would read as a *smaller* book.
    """

    model_config = ConfigDict(frozen=True)

    # Index-equivalent dollars of directional exposure. Positive = long.
    beta_weighted_delta: float | None
    # Dollars per one point of implied volatility. Negative = short vol.
    net_vega: float | None
    # Legs that had to be skipped, carried so a caller can say why an aggregate
    # came back unknown.
    incomplete_legs: int = 0

    @classmethod
    def unknown(cls, incomplete_legs: int = 0) -> Exposure:
        return cls(beta_weighted_delta=None, net_vega=None, incomplete_legs=incomplete_legs)

    def combined_with(self, other: Exposure) -> Exposure:
        """This exposure plus ``other``, unknown if either side is unknown."""

        def add(left: float | None, right: float | None) -> float | None:
            return None if left is None or right is None else left + right

        return Exposure(
            beta_weighted_delta=add(self.beta_weighted_delta, other.beta_weighted_delta),
            net_vega=add(self.net_vega, other.net_vega),
            incomplete_legs=self.incomplete_legs + other.incomplete_legs,
        )


def plan_exposure(
    plan: OrderPlan,
    *,
    spot: float,
    iv: float | None,
    risk_free_rate: float,
    today: date | None = None,
) -> Exposure:
    """Exposure the plan would add if it filled.

    Delta comes off the legs, which already carry it from the chain (or from
    ``bs_delta`` when the chain had no greeks) — sizing and risk must not
    re-derive a number the builder already selected the contract on. Vega is
    computed here because it is not on ``Leg`` and depends on the same spot and
    ATM vol as the book side, keeping the two comparable.
    """
    reference = today or date.today()
    beta = beta_for(plan.underlying)
    dollar_delta = 0.0
    vega = 0.0
    missing_delta = False
    missing_vega = False
    incomplete = 0

    for leg in plan.legs:
        sign = 1.0 if leg.side == "buy" else -1.0
        contracts = sign * leg.ratio * plan.qty
        if leg.delta is None:
            missing_delta = True
            incomplete += 1
        else:
            dollar_delta += leg.delta * contracts * _CONTRACT_MULTIPLIER * spot * beta
        leg_vega = bs_vega(
            spot=spot,
            strike=leg.strike,
            dte_days=(leg.expiry - reference).days,
            iv=iv,
            risk_free_rate=risk_free_rate,
        )
        if leg_vega is None:
            missing_vega = True
            incomplete += 1
        else:
            vega += leg_vega * contracts

    return Exposure(
        beta_weighted_delta=None if missing_delta else dollar_delta,
        net_vega=None if missing_vega else vega,
        incomplete_legs=incomplete,
    )


def book_exposure(
    positions: list[dict[str, Any]],
    *,
    market_by_symbol: Mapping[str, tuple[float, float | None]],
    risk_free_rate: float,
    today: date | None = None,
    greeks_by_symbol: Mapping[str, tuple[float | None, float | None]] | None = None,
) -> Exposure:
    """Exposure of the open option book, from the broker's position list.

    ``greeks_by_symbol`` is the live Alpaca option snapshot (delta, dollar
    vega per contract), keyed by OCC symbol. That is the primary source —
    ``get_option_snapshot`` already returns both greeks. Black-Scholes from
    the evidence pack's ATM vol is only a fallback for a missing field.

    Each Greek is independent: a missing vega does not erase a known delta.
    A field that is still missing after both sources stays ``None``; the
    risk gate skips that cap rather than rejecting the order.
    """
    reference = today or date.today()
    dollar_delta = 0.0
    vega = 0.0
    missing_delta = False
    missing_vega = False
    incomplete = 0
    snapshots = greeks_by_symbol or {}

    for position in positions:
        if position.get("asset_class") != "us_option":
            continue
        occ = str(position.get("symbol") or "")
        parsed = parse_occ_symbol(occ)
        quantity = finite_float(position.get("qty"))
        if parsed is None or quantity is None:
            missing_delta = True
            missing_vega = True
            incomplete += 1
            continue

        snap_delta, snap_vega = snapshots.get(occ, (None, None))
        market = market_by_symbol.get(parsed.underlying.upper())
        spot = market[0] if market is not None else None
        iv = market[1] if market is not None else None
        dte = (parsed.expiry - reference).days

        delta = snap_delta
        if delta is None and spot is not None:
            delta = _bs_delta(
                spot=spot,
                strike=parsed.strike,
                dte_days=dte,
                iv=iv,
                option_type=parsed.option_type,
                risk_free_rate=risk_free_rate,
            )
        leg_vega = snap_vega
        if leg_vega is None and spot is not None:
            leg_vega = bs_vega(
                spot=spot,
                strike=parsed.strike,
                dte_days=dte,
                iv=iv,
                risk_free_rate=risk_free_rate,
            )

        if delta is None or spot is None:
            missing_delta = True
            incomplete += 1
        else:
            dollar_delta += delta * quantity * _CONTRACT_MULTIPLIER * spot * beta_for(
                parsed.underlying
            )

        if leg_vega is None:
            missing_vega = True
            incomplete += 1
        else:
            vega += leg_vega * quantity

    return Exposure(
        beta_weighted_delta=None if missing_delta else dollar_delta,
        net_vega=None if missing_vega else vega,
        incomplete_legs=incomplete,
    )


def _bs_delta(
    *,
    spot: float,
    strike: float,
    dte_days: int,
    iv: float | None,
    option_type: str,
    risk_free_rate: float,
) -> float | None:
    """Thin adapter over ``strategy_builder.bs_delta``.

    Imported lazily: strategy_builder imports sizing, sizing does not import
    this module, and keeping the import inside the call avoids adding a
    module-level edge between the risk side and the construction side of the
    codebase for one function.
    """
    from options_m.strategy_builder import bs_delta

    if option_type not in ("call", "put"):
        return None
    return bs_delta(
        spot=spot,
        strike=strike,
        dte_days=dte_days,
        iv=iv,
        option_type=option_type,  # type: ignore[arg-type]
        risk_free_rate=risk_free_rate,
    )


def market_from_evidence(payload: dict[str, Any]) -> tuple[float, float | None] | None:
    """``(spot, iv_atm)`` from one cached evidence pack, or ``None``.

    The spot preference is ``last`` → ``day_close`` → ``mid``, the same order
    ``EvidenceCollector._spot`` itself uses to pick the price it does its option
    maths against — the two must agree or the greeks here would be computed
    against a different underlying price than the one the pack was built on.
    Every field can be the literal string ``MISSING``, which ``finite_float``
    reads as absent.
    """
    spot_block = payload.get("spot")
    if not isinstance(spot_block, dict):
        return None
    spot: float | None = None
    for key in ("last", "day_close", "mid"):
        spot = finite_float(spot_block.get(key))
        if spot is not None and spot > 0:
            break
        spot = None
    if spot is None:
        return None

    options_block = payload.get("options")
    iv = finite_float(options_block.get("iv_atm")) if isinstance(options_block, dict) else None
    return spot, iv
