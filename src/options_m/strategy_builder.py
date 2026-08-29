"""Turns a :class:`StrategyIntent` into a real, risk-computable order plan.

The core anti-hallucination component: the caller (an LLM in Phase 3, a
hand-written intent in this phase) states direction, target delta, a DTE
window and a structure — never a contract. This module selects the real
contracts closest to that intent from the live chain and prices them. If a
finite ``max_loss`` cannot be computed for real, the answer is a
:class:`Rejection`, never a guess.

Two live calls feed this, joined by OCC symbol:

* ``AlpacaMcp.get_option_contracts`` — strike, expiry, type, open interest.
  No quotes.
* ``AlpacaMcp.get_option_chain`` — quotes, greeks, implied volatility, keyed
  by OCC symbol. No strike/expiry/open interest.

Neither call alone has enough to build or price a leg; ``normalize_contracts``
does the join once, so nothing downstream has to guess at either shape.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from typing import Any, Literal

from options_m.config import Settings
from options_m.mcp_client import finite_float
from options_m.models import Leg, OrderPlan, Rejection, StrategyIntent

_CALL_STRATEGIES = frozenset({"long_call", "debit_call_spread", "covered_call"})
_PUT_STRATEGIES = frozenset({"long_put", "debit_put_spread", "cash_secured_put"})
_VERTICAL_STRATEGIES = frozenset({"debit_call_spread", "debit_put_spread"})
_SHORT_ONLY_STRATEGIES = frozenset({"covered_call", "cash_secured_put"})
_CONTRACT_MULTIPLIER = 100.0


@dataclass(frozen=True, slots=True)
class NormalizedContract:
    symbol: str
    strike: float
    expiry: date
    option_type: Literal["call", "put"]
    open_interest: int | None
    delta: float | None
    delta_source: Literal["chain", "black_scholes"] | None
    implied_volatility: float | None
    bid: float | None
    ask: float | None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(
    *,
    spot: float,
    strike: float,
    dte_days: int,
    iv: float | None,
    option_type: Literal["call", "put"],
    risk_free_rate: float,
) -> float | None:
    """Black-Scholes delta, ``q=0`` (no dividend yield).

    A documented simplification, not a placeholder: this is a first-class,
    independently tested fallback for when the chain snapshot carries no
    greeks. Returns ``None`` for any input that makes the formula undefined —
    never a fabricated number.
    """
    if spot <= 0 or strike <= 0 or iv is None or iv <= 0 or dte_days <= 0:
        return None
    t = dte_days / 365.0
    try:
        d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * iv * iv) * t) / (
            iv * math.sqrt(t)
        )
    except (ValueError, ZeroDivisionError):
        return None
    return _norm_cdf(d1) if option_type == "call" else _norm_cdf(d1) - 1.0


def _is_standard_monthly(expiry: date) -> bool:
    """Whether ``expiry`` is the third Friday of its month."""
    first_of_month = expiry.replace(day=1)
    first_friday = first_of_month + timedelta(days=(4 - first_of_month.weekday()) % 7)
    third_friday = first_friday + timedelta(days=14)
    return expiry == third_friday


def normalize_contracts(
    contracts: list[dict[str, Any]], snapshots: dict[str, dict[str, Any]]
) -> list[NormalizedContract]:
    """Join contract metadata with live quotes/greeks by OCC symbol.

    A contract that fails to parse (missing/malformed strike, expiry, or
    type) is dropped, not guessed at — it simply cannot be selected, which
    surfaces downstream as a normal ``Rejection`` if nothing usable remains.
    """
    normalized: list[NormalizedContract] = []
    for contract in contracts:
        symbol = contract.get("symbol")
        option_type = contract.get("type")
        if not isinstance(symbol, str) or option_type not in ("call", "put"):
            continue
        if contract.get("tradable") is False:
            continue
        status = contract.get("status")
        if status is not None and status != "active":
            continue
        try:
            strike = float(contract["strike_price"])
            expiry = date.fromisoformat(contract["expiration_date"])
        except (KeyError, TypeError, ValueError):
            continue

        raw_oi = contract.get("open_interest")
        open_interest = None
        if raw_oi is not None:
            try:
                open_interest = int(float(raw_oi))
            except (TypeError, ValueError):
                open_interest = None

        snapshot = snapshots.get(symbol) or {}
        greeks = snapshot.get("greeks")
        delta = finite_float(greeks.get("delta")) if isinstance(greeks, dict) else None
        iv = finite_float(snapshot.get("impliedVolatility"))
        quote = snapshot.get("latestQuote")
        bid = finite_float(quote.get("bp")) if isinstance(quote, dict) else None
        ask = finite_float(quote.get("ap")) if isinstance(quote, dict) else None

        normalized.append(
            NormalizedContract(
                symbol=symbol,
                strike=strike,
                expiry=expiry,
                option_type=option_type,
                open_interest=open_interest,
                delta=delta,
                delta_source="chain" if delta is not None else None,
                implied_volatility=iv,
                bid=bid,
                ask=ask,
            )
        )
    return normalized


def _effective_delta(
    contract: NormalizedContract, *, spot: float, risk_free_rate: float
) -> tuple[float, Literal["chain", "black_scholes"]] | None:
    if contract.delta is not None:
        return contract.delta, "chain"
    dte_days = (contract.expiry - date.today()).days
    delta = bs_delta(
        spot=spot,
        strike=contract.strike,
        dte_days=dte_days,
        iv=contract.implied_volatility,
        option_type=contract.option_type,
        risk_free_rate=risk_free_rate,
    )
    return (delta, "black_scholes") if delta is not None else None


def _reject(proposal_id: int, reason: str, **detail: Any) -> Rejection:
    return Rejection(proposal_id=proposal_id, reason=reason, detail=detail)


def _leg_from_contract(
    contract: NormalizedContract,
    *,
    side: Literal["buy", "sell"],
    delta: float | None,
    delta_source: Literal["chain", "black_scholes"] | None,
) -> Leg:
    return Leg(
        symbol=contract.symbol,
        side=side,
        strike=contract.strike,
        expiry=contract.expiry,
        option_type=contract.option_type,
        delta=delta,
        delta_source=delta_source,
        bid=contract.bid,
        ask=contract.ask,
        open_interest=contract.open_interest,
    )


def _liquidity_rejection(
    proposal_id: int, contract: NormalizedContract, *, max_spread_pct: float, min_oi: int
) -> Rejection | None:
    if contract.bid is None or contract.ask is None or contract.bid <= 0 or contract.ask <= 0:
        return _reject(proposal_id, "missing_or_zero_quote", symbol=contract.symbol)
    mid = (contract.bid + contract.ask) / 2
    if mid > 0 and (contract.ask - contract.bid) / mid > max_spread_pct:
        spread_pct = (contract.ask - contract.bid) / mid
        return _reject(proposal_id, "wide_spread", symbol=contract.symbol, spread_pct=spread_pct)
    if contract.open_interest is None or contract.open_interest < min_oi:
        return _reject(
            proposal_id,
            "low_open_interest",
            symbol=contract.symbol,
            open_interest=contract.open_interest,
        )
    return None


async def build(
    intent: StrategyIntent,
    *,
    contracts: list[dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
    account: dict[str, Any],
    existing_position: dict[str, Any] | None,
    settings: Settings,
    proposal_id: int,
    spot: float,
) -> OrderPlan | Rejection:
    """Build a fully-priced :class:`OrderPlan`, or explain why one is refused."""
    universe = normalize_contracts(contracts, snapshots)
    wants_call = intent.strategy in _CALL_STRATEGIES
    option_type: Literal["call", "put"] = "call" if wants_call else "put"

    candidates = [
        contract
        for contract in universe
        if contract.option_type == option_type
        and intent.dte_min <= (contract.expiry - date.today()).days <= intent.dte_max
    ]
    if not candidates:
        return _reject(
            proposal_id,
            "no_contracts_in_window",
            underlying=intent.underlying,
            dte_min=intent.dte_min,
            dte_max=intent.dte_max,
        )

    scored: list[tuple[float, bool, NormalizedContract, float, Literal["chain", "black_scholes"]]]
    scored = []
    for contract in candidates:
        effective = _effective_delta(contract, spot=spot, risk_free_rate=settings.risk_free_rate)
        if effective is None:
            continue
        delta, source = effective
        diff = abs(abs(delta) - intent.target_delta)
        prefer_standard = not (
            settings.standard_monthly_expiry_preference and _is_standard_monthly(contract.expiry)
        )
        scored.append((diff, prefer_standard, contract, delta, source))
    if not scored:
        return _reject(proposal_id, "no_delta_data_available", underlying=intent.underlying)
    scored.sort(key=lambda row: (row[0], row[1]))
    _diff, _prefer, primary, primary_delta, primary_source = scored[0]

    primary_side: Literal["buy", "sell"] = (
        "sell" if intent.strategy in _SHORT_ONLY_STRATEGIES else "buy"
    )
    rejection = _liquidity_rejection(
        proposal_id,
        primary,
        max_spread_pct=settings.max_spread_pct,
        min_oi=settings.min_open_interest,
    )
    if rejection is not None:
        return rejection

    legs = [
        _leg_from_contract(
            primary, side=primary_side, delta=primary_delta, delta_source=primary_source
        )
    ]

    secondary: NormalizedContract | None = None
    if intent.strategy in _VERTICAL_STRATEGIES:
        if intent.spread_width is None:
            return _reject(proposal_id, "missing_spread_width")
        target_strike = (
            primary.strike + intent.spread_width
            if intent.strategy == "debit_call_spread"
            else primary.strike - intent.spread_width
        )
        same_expiry = sorted(
            {
                c.strike
                for c in candidates
                if c.expiry == primary.expiry and c.strike != primary.strike
            }
        )
        if not same_expiry:
            return _reject(proposal_id, "no_strike_within_increment", target=target_strike)
        all_strikes = sorted({c.strike for c in candidates if c.expiry == primary.expiry})
        increment = min(b - a for a, b in pairwise(all_strikes))
        nearest = min(same_expiry, key=lambda strike: abs(strike - target_strike))
        if abs(nearest - target_strike) > increment:
            return _reject(
                proposal_id,
                "no_strike_within_increment",
                target=target_strike,
                nearest=nearest,
                increment=increment,
            )
        secondary = next(
            c for c in candidates if c.expiry == primary.expiry and c.strike == nearest
        )
        rejection = _liquidity_rejection(
            proposal_id,
            secondary,
            max_spread_pct=settings.max_spread_pct,
            min_oi=settings.min_open_interest,
        )
        if rejection is not None:
            return rejection
        legs.append(_leg_from_contract(secondary, side="sell", delta=None, delta_source=None))

    entry_price, rejection = _price(intent, settings, primary, secondary, proposal_id)
    if rejection is not None:
        return rejection
    assert entry_price is not None  # noqa: S101 - _price returns exactly one of the two

    max_loss, max_profit, breakeven, rejection = _risk_profile(
        intent, proposal_id, primary, secondary, entry_price, spot, existing_position
    )
    if rejection is not None:
        return rejection
    assert max_loss is not None  # noqa: S101 - _risk_profile returns exactly one of the two

    equity = finite_float(account.get("equity"))
    if equity is None:
        return _reject(proposal_id, "no_account_equity")
    max_premium_budget = settings.max_premium_pct_per_trade * equity
    qty = math.floor(max_premium_budget / max_loss)

    if intent.strategy == "cash_secured_put":
        cash = finite_float(account.get("cash"))
        if cash is not None:
            max_qty_by_cash = math.floor(cash / (primary.strike * _CONTRACT_MULTIPLIER))
            qty = min(qty, max_qty_by_cash)

    if qty <= 0:
        return _reject(
            proposal_id,
            "zero_quantity",
            max_premium_budget=max_premium_budget,
            max_loss_per_contract=max_loss,
        )

    client_order_id = f"om-{proposal_id}"
    return OrderPlan(
        proposal_id=proposal_id,
        underlying=intent.underlying,
        strategy=intent.strategy,
        legs=legs,
        qty=qty,
        limit_price=entry_price,
        max_loss=max_loss * qty,
        max_profit=max_profit * qty if max_profit is not None else None,
        breakeven=breakeven,
        client_order_id=client_order_id,
    )


def _price(
    intent: StrategyIntent,
    settings: Settings,
    primary: NormalizedContract,
    secondary: NormalizedContract | None,
    proposal_id: int,
) -> tuple[float | None, Rejection | None]:
    """Net entry price: a positive number either way — the sign that matters
    (debit vs. credit) is implied by ``primary_side``, not stored twice."""
    assert primary.bid is not None and primary.ask is not None  # noqa: S101 - liquidity-gated above
    primary_mid = (primary.bid + primary.ask) / 2
    nudge = settings.limit_price_spread_nudge_pct

    if secondary is not None:
        assert secondary.bid is not None and secondary.ask is not None  # noqa: S101
        secondary_mid = (secondary.bid + secondary.ask) / 2
        net_mid = primary_mid - secondary_mid
        if net_mid <= 0:
            return None, _reject(proposal_id, "not_a_debit", net_mid=net_mid)
        width = (primary.ask - primary.bid) + (secondary.ask - secondary.bid)
        return net_mid + nudge * width, None

    if intent.strategy in _SHORT_ONLY_STRATEGIES:
        # Selling: the aggressive side is a lower price, closer to the bid.
        return primary_mid - nudge * (primary.ask - primary.bid), None

    return primary_mid + nudge * (primary.ask - primary.bid), None


def _risk_profile(
    intent: StrategyIntent,
    proposal_id: int,
    primary: NormalizedContract,
    secondary: NormalizedContract | None,
    entry_price: float,
    spot: float,
    existing_position: dict[str, Any] | None,
) -> tuple[float | None, float | None, float | None, Rejection | None]:
    """Per-contract ``(max_loss, max_profit, breakeven)`` in dollars, or a Rejection."""
    strategy = intent.strategy
    mult = _CONTRACT_MULTIPLIER

    if strategy in ("long_call", "long_put"):
        max_loss = entry_price * mult
        if strategy == "long_call":
            breakeven = primary.strike + entry_price
        else:
            breakeven = primary.strike - entry_price
        return max_loss, None, breakeven, None

    if strategy in _VERTICAL_STRATEGIES:
        assert secondary is not None  # noqa: S101 - verticals always build a secondary leg
        if intent.spread_width is None:
            return None, None, None, _reject(proposal_id, "missing_spread_width")
        width = intent.spread_width
        if entry_price >= width:
            return None, None, None, _reject(
                proposal_id, "net_debit_exceeds_width", net_debit=entry_price, width=width
            )
        max_loss = entry_price * mult
        max_profit = (width - entry_price) * mult
        breakeven = (
            primary.strike + entry_price
            if strategy == "debit_call_spread"
            else primary.strike - entry_price
        )
        return max_loss, max_profit, breakeven, None

    if strategy == "covered_call":
        shares = finite_float(existing_position.get("qty")) if existing_position else None
        side = existing_position.get("side") if existing_position else None
        if shares is None or shares < 100 or side == "short":
            return None, None, None, _reject(proposal_id, "no_underlying_shares")
        premium = entry_price
        max_loss = max(0.0, spot * mult - premium * mult)
        if max_loss <= 0:
            return None, None, None, _reject(proposal_id, "undefined_risk")
        max_profit = (
            (primary.strike - spot) * mult + premium * mult
            if primary.strike > spot
            else premium * mult
        )
        breakeven = spot - premium
        return max_loss, max_profit, breakeven, None

    # cash_secured_put
    premium = entry_price
    max_loss = (primary.strike - premium) * mult
    if max_loss <= 0:
        return None, None, None, _reject(proposal_id, "undefined_risk")
    max_profit = premium * mult
    breakeven = primary.strike - premium
    return max_loss, max_profit, breakeven, None
