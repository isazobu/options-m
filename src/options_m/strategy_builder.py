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
from options_m.volatility import implied_vol

# Dividend yield assumed away for the IV solve, matching evidence.py's own
# fallback so the two modules recover the same sigma from the same quote. The
# risk-free rate is passed in instead, so the vol we solve for and the delta we
# then compute from it come from one consistent model.
_DIVIDEND_YIELD = 0.0

_CALL_STRATEGIES = frozenset({"long_call", "debit_call_spread", "covered_call"})
_PUT_STRATEGIES = frozenset({"long_put", "debit_put_spread", "cash_secured_put"})
_VERTICAL_STRATEGIES = frozenset({"debit_call_spread", "debit_put_spread"})
_SHORT_ONLY_STRATEGIES = frozenset({"covered_call", "cash_secured_put"})
_CONTRACT_MULTIPLIER = 100.0

# matrix.py names the two debit verticals the other way round from the names
# this module grew up with. Same structure, same legs — canonicalised on the
# way in so there is exactly one spelling below this line.
_STRATEGY_ALIASES = {
    "call_debit_spread": "debit_call_spread",
    "put_debit_spread": "debit_put_spread",
}

# What this module can actually assemble and price. Every name the Strategy
# Matrix can emit now has a builder. Anything *not* listed here must still be
# refused by name rather than allowed through: an unrecognised strategy is not
# in _CALL_STRATEGIES, so option_type would default to "put"; it is not in
# _VERTICAL_STRATEGIES, so no second leg would be built; and _risk_profile's
# unguarded tail would price it as a cash-secured put. A four-leg condor would
# have been submitted as a lone put. Submitting a wrong structure is far worse
# than submitting nothing.
_SUPPORTED_STRATEGIES = frozenset(
    {
        "long_call",
        "long_put",
        "debit_call_spread",
        "debit_put_spread",
        "covered_call",
        "cash_secured_put",
        "long_strangle",
        "put_credit_spread",
        "call_credit_spread",
        "iron_condor",
        "iron_butterfly",
    }
)

# Structures whose net is a credit received rather than a debit paid. They
# share one builder shape and one sign rule: the net is carried positive
# throughout this module and negated exactly once, when the OrderPlan is
# constructed. See _build_credit_structure.
_CREDIT_STRATEGIES = frozenset(
    {"put_credit_spread", "call_credit_spread", "iron_condor", "iron_butterfly"}
)

# Structures whose short legs sit at the money rather than at a target delta.
# They collect far more premium per unit of width, so they need much wider
# wings before a profit zone exists at all — see _move_mult.
_ATM_SHORT_STRATEGIES = frozenset({"iron_butterfly"})


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
    contracts: list[dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
    *,
    spot: float | None = None,
    risk_free_rate: float = 0.0,
) -> list[NormalizedContract]:
    """Join contract metadata with live quotes/greeks by OCC symbol.

    A contract that fails to parse (missing/malformed strike, expiry, or
    type) is dropped, not guessed at — it simply cannot be selected, which
    surfaces downstream as a normal ``Rejection`` if nothing usable remains.

    ``spot`` enables the implied-vol fallback. Alpaca's option snapshots carry
    ``greeks`` and ``impliedVolatility`` only on the OPRA feed; on the feed a
    paper account gets, both come back ``None`` for every contract. Without a
    fallback that makes delta unrecoverable and every selection ends in
    ``no_delta_data_available``. Solving sigma from the quote mid is the same
    thing ``evidence.py`` already does for its IV/RV read, so the two agree on
    where a missing IV comes from.
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

        if iv is None and spot is not None and bid and ask and bid > 0 and ask > 0:
            dte_days = (expiry - date.today()).days
            if dte_days > 0:
                iv = implied_vol(
                    (bid + ask) / 2,
                    spot,
                    strike,
                    dte_days / 365.0,
                    risk_free_rate,
                    _DIVIDEND_YIELD,
                    option_type,
                )

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
    proposal_id: int,
    contract: NormalizedContract,
    *,
    max_spread_pct: float,
    max_spread_abs: float,
    min_oi: int,
) -> Rejection | None:
    """``None`` when the contract is tradeable; a :class:`Rejection` otherwise.

    A leg is refused as wide only when it is wide in *both* senses. A cheap
    protective wing quoted 0.10/0.15 is 40% wide on a relative measure but
    costs five cents to cross, and refusing it on the percentage alone would
    make every iron structure unbuildable — while the wing is the very leg
    that makes the structure defined-risk.
    """
    if contract.bid is None or contract.ask is None or contract.bid <= 0 or contract.ask <= 0:
        return _reject(proposal_id, "missing_or_zero_quote", symbol=contract.symbol)
    mid = (contract.bid + contract.ask) / 2
    spread_abs = contract.ask - contract.bid
    if mid > 0 and spread_abs / mid > max_spread_pct and spread_abs > max_spread_abs:
        return _reject(
            proposal_id,
            "wide_spread",
            symbol=contract.symbol,
            spread_pct=spread_abs / mid,
            spread_abs=spread_abs,
        )
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
    canonical = _STRATEGY_ALIASES.get(intent.strategy, intent.strategy)
    if canonical not in _SUPPORTED_STRATEGIES:
        return _reject(
            proposal_id,
            "unsupported_strategy",
            strategy=intent.strategy,
            supported=sorted(_SUPPORTED_STRATEGIES),
        )
    if canonical != intent.strategy:
        intent = intent.model_copy(update={"strategy": canonical})

    universe = normalize_contracts(
        contracts, snapshots, spot=spot, risk_free_rate=settings.risk_free_rate
    )

    # A strangle is selected on a different axis from everything below: two
    # legs of *different* option types sharing one expiry, rather than one
    # primary contract with an optional same-type partner. It gets its own
    # builder instead of being bent through the single-type funnel.
    if intent.strategy == "long_strangle":
        return _build_long_strangle(
            intent,
            universe=universe,
            account=account,
            settings=settings,
            proposal_id=proposal_id,
            spot=spot,
        )

    # Credit structures invert the buy/sell order the funnel below assumes
    # (short anchor, long wing), and the two irons are a pair of verticals of
    # *different* option types. Neither fits "one primary contract with an
    # optional same-type partner", so they get their own builder for the same
    # reason the strangle does.
    if intent.strategy in _CREDIT_STRATEGIES:
        return _build_credit_structure(
            intent,
            universe=universe,
            account=account,
            settings=settings,
            proposal_id=proposal_id,
            spot=spot,
        )

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
        max_spread_abs=settings.max_spread_abs,
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
        same_expiry_contracts = [c for c in candidates if c.expiry == primary.expiry]
        width = _target_width(
            intent,
            same_expiry_contracts,
            settings=settings,
            spot=spot,
            dte_days=(primary.expiry - date.today()).days,
        )
        if width is None or width <= 0:
            return _reject(proposal_id, "missing_spread_width", strategy=intent.strategy)
        target_strike = (
            primary.strike + width
            if intent.strategy == "debit_call_spread"
            else primary.strike - width
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
            max_spread_abs=settings.max_spread_abs,
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
        intent, proposal_id, primary, secondary, entry_price, spot, existing_position, settings
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


def _build_long_strangle(
    intent: StrategyIntent,
    *,
    universe: list[NormalizedContract],
    account: dict[str, Any],
    settings: Settings,
    proposal_id: int,
    spot: float,
) -> OrderPlan | Rejection:
    """Buy an OTM call and an OTM put on the same expiry.

    The matrix reaches for this in a flat trend with cheap premium: no
    directional view, paying for movement in either direction. Both legs are
    long, so max loss is the debit and Alpaca's naked-short-leg validation is
    not in play at all.
    """
    today = date.today()
    in_window = [
        contract
        for contract in universe
        if intent.dte_min <= (contract.expiry - today).days <= intent.dte_max
    ]
    if not in_window:
        return _reject(
            proposal_id,
            "no_contracts_in_window",
            underlying=intent.underlying,
            dte_min=intent.dte_min,
            dte_max=intent.dte_max,
        )

    # Both legs must share an expiry, so the expiry is chosen first: the one
    # whose best call and best put together sit closest to the target delta.
    best: tuple[float, NormalizedContract, float, NormalizedContract, float] | None = None
    for expiry in sorted({contract.expiry for contract in in_window}):
        same_expiry = [contract for contract in in_window if contract.expiry == expiry]
        call = _closest_to_delta(same_expiry, "call", intent.target_delta, settings, spot)
        put = _closest_to_delta(same_expiry, "put", intent.target_delta, settings, spot)
        if call is None or put is None:
            continue
        call_contract, call_delta, call_diff = call
        put_contract, put_delta, put_diff = put
        # A strangle needs the two strikes apart; equal strikes are a straddle,
        # a different structure with a different risk profile.
        if call_contract.strike <= put_contract.strike:
            continue
        penalty = (
            0.0
            if not settings.standard_monthly_expiry_preference or _is_standard_monthly(expiry)
            else 1e-6
        )
        score = call_diff + put_diff + penalty
        if best is None or score < best[0]:
            best = (score, call_contract, call_delta, put_contract, put_delta)

    if best is None:
        return _reject(proposal_id, "no_strangle_pair_available", underlying=intent.underlying)
    _score, call_contract, call_delta, put_contract, put_delta = best

    for contract in (call_contract, put_contract):
        rejection = _liquidity_rejection(
            proposal_id,
            contract,
            max_spread_pct=settings.max_spread_pct,
            max_spread_abs=settings.max_spread_abs,
            min_oi=settings.min_open_interest,
        )
        if rejection is not None:
            return rejection

    assert call_contract.bid is not None and call_contract.ask is not None  # noqa: S101
    assert put_contract.bid is not None and put_contract.ask is not None  # noqa: S101
    call_mid = (call_contract.bid + call_contract.ask) / 2
    put_mid = (put_contract.bid + put_contract.ask) / 2
    width = (call_contract.ask - call_contract.bid) + (put_contract.ask - put_contract.bid)
    entry_price = call_mid + put_mid + settings.limit_price_spread_nudge_pct * width

    max_loss = entry_price * _CONTRACT_MULTIPLIER
    equity = finite_float(account.get("equity"))
    if equity is None:
        return _reject(proposal_id, "no_account_equity")
    qty = math.floor(settings.max_premium_pct_per_trade * equity / max_loss)
    if qty <= 0:
        return _reject(
            proposal_id,
            "zero_quantity",
            max_premium_budget=settings.max_premium_pct_per_trade * equity,
            max_loss_per_contract=max_loss,
        )

    return OrderPlan(
        proposal_id=proposal_id,
        underlying=intent.underlying,
        strategy=intent.strategy,
        legs=[
            _leg_from_contract(
                call_contract, side="buy", delta=call_delta, delta_source="black_scholes"
            ),
            _leg_from_contract(
                put_contract, side="buy", delta=put_delta, delta_source="black_scholes"
            ),
        ],
        qty=qty,
        limit_price=entry_price,
        max_loss=max_loss * qty,
        # Unbounded on the upside, and the downside is capped only at a zero
        # underlying — there is no honest single max_profit here.
        max_profit=None,
        # A strangle has two breakevens (call strike + debit, put strike -
        # debit). One field cannot hold both, and reporting either alone reads
        # as "the" breakeven, so this stays empty rather than half-true.
        breakeven=None,
        client_order_id=f"om-{proposal_id}",
    )


@dataclass(frozen=True, slots=True)
class _CreditVertical:
    """A short anchor plus the protective long wing submitted alongside it.

    The wing is not optional and not a separate order: Alpaca's multi-leg
    endpoint rejects a naked short inside a multi-leg order, so pairing them
    here is both the risk rule and an API constraint.
    """

    short: NormalizedContract
    short_delta: float | None
    short_delta_source: Literal["chain", "black_scholes"] | None
    long: NormalizedContract
    score: float

    @property
    def width(self) -> float:
        """Realised distance between the strikes, not the width requested.

        The two differ whenever the wing snapped to a listed strike that is
        not exactly ``spread_width`` away, and every dollar figure below has
        to be computed from the contracts actually being submitted.
        """
        return abs(self.short.strike - self.long.strike)

    def legs(self) -> list[Leg]:
        return [
            _leg_from_contract(
                self.short,
                side="sell",
                delta=self.short_delta,
                delta_source=self.short_delta_source,
            ),
            _leg_from_contract(self.long, side="buy", delta=None, delta_source=None),
        ]

    def contracts(self) -> tuple[NormalizedContract, NormalizedContract]:
        return self.short, self.long


def _expected_move(spot: float, iv: float, dte_days: int) -> float | None:
    """One standard deviation of the underlying over the option's life."""
    if spot <= 0 or iv <= 0 or dte_days <= 0:
        return None
    return spot * iv * math.sqrt(dte_days / 365.0)


def _atm_implied_vol(contracts: list[NormalizedContract], spot: float) -> float | None:
    """Implied vol of the listed strike nearest spot, or ``None`` if unknown."""
    candidates = [
        (abs(contract.strike - spot), iv)
        for contract in contracts
        if (iv := contract.implied_volatility) is not None and iv > 0
    ]
    if not candidates:
        return None
    return min(candidates)[1]


def _move_mult(strategy: str, settings: Settings) -> float:
    """Wing distance in expected moves, by where the short legs sit.

    An at-the-money short collects far more premium than a 0.25-delta one, so
    the same wing distance leaves it a far smaller profit zone. Measured on
    real SPY and NVDA chains, one multiplier could not serve both families:
    the value that keeps a credit vertical above the thin-credit floor puts an
    iron butterfly at 96% of its width.
    """
    if strategy in _ATM_SHORT_STRATEGIES:
        return settings.spread_width_expected_move_mult_atm
    return settings.spread_width_expected_move_mult


def _target_width(
    intent: StrategyIntent,
    contracts: list[NormalizedContract],
    *,
    settings: Settings,
    spot: float,
    dte_days: int,
) -> float | None:
    """The wing distance to aim for, or ``None`` when it cannot be determined.

    A width pinned on the intent is honoured exactly as given — that is the
    CLI's ``--spread-width`` and any caller that knows what it wants.
    Otherwise the wings are sized from the expected move over the option's
    life, because a flat dollar width is only correct for one underlying at
    one vol level: on SPY at 769 with 26 days to run, $5 wings sit a fifth of
    an expected move from the short, which is why an at-the-money butterfly
    built that way collected almost its entire width and had no profit zone
    left to speak of.
    """
    if intent.spread_width is not None:
        return intent.spread_width
    mult = _move_mult(intent.strategy, settings)
    if mult <= 0:
        return None
    iv = _atm_implied_vol(contracts, spot)
    if iv is None:
        return None
    move = _expected_move(spot, iv, dte_days)
    return move * mult if move is not None else None


def _strike_increment(contracts: list[NormalizedContract]) -> float | None:
    """Smallest gap in the strike ladder, or ``None`` below two strikes.

    Guarding the two-strike minimum here keeps ``min()`` off an empty
    generator no matter how the caller filtered its slice.
    """
    strikes = sorted({contract.strike for contract in contracts})
    if len(strikes) < 2:
        return None
    return min(b - a for a, b in pairwise(strikes))


def _nearest_listed_strike(
    candidates: list[NormalizedContract], target_strike: float, *, increment: float
) -> NormalizedContract | None:
    """The candidate nearest ``target_strike``, or ``None`` if none is within
    one strike increment — a wing further away than that is a different
    structure from the one the intent asked for."""
    if not candidates:
        return None
    nearest = min(candidates, key=lambda contract: abs(contract.strike - target_strike))
    if abs(nearest.strike - target_strike) > increment:
        return None
    return nearest


def _credit_vertical(
    same_expiry: list[NormalizedContract],
    option_type: Literal["call", "put"],
    *,
    target_delta: float,
    width: float,
    increment: float,
    settings: Settings,
    spot: float,
    short_strike: float | None = None,
) -> _CreditVertical | None:
    """One credit vertical on a single expiry, or ``None`` if unassemblable.

    ``short_strike`` pins the anchor instead of selecting it by delta, which
    is what an iron butterfly needs — both its shorts sit on the same
    at-the-money strike rather than at a target delta.
    """
    side = [contract for contract in same_expiry if contract.option_type == option_type]
    if not side:
        return None

    if short_strike is None:
        picked = _closest_to_delta(side, option_type, target_delta, settings, spot)
        if picked is None:
            return None
        short, short_delta, score = picked
    else:
        anchored = [contract for contract in side if contract.strike == short_strike]
        if not anchored:
            return None
        short = anchored[0]
        short_delta = None
        score = abs(short.strike - spot) / spot if spot > 0 else 0.0

    effective = _effective_delta(short, spot=spot, risk_free_rate=settings.risk_free_rate)
    if effective is not None:
        short_delta, short_delta_source = effective
    else:
        short_delta_source = None

    # The wing is always further out of the money than the short it protects:
    # below for puts, above for calls.
    if option_type == "put":
        target = short.strike - width
        wing_side = [contract for contract in side if contract.strike < short.strike]
    else:
        target = short.strike + width
        wing_side = [contract for contract in side if contract.strike > short.strike]

    wing = _nearest_listed_strike(wing_side, target, increment=increment)
    if wing is None:
        return None
    return _CreditVertical(
        short=short,
        short_delta=short_delta,
        short_delta_source=short_delta_source,
        long=wing,
        score=score,
    )


def _net_credit(verticals: list[_CreditVertical], settings: Settings) -> float:
    """Net credit received per contract, carried **positive**.

    The sign is inverted exactly once, where the ``OrderPlan`` is built. We
    are net sellers here, so the aggressive side of the market is the lower
    price and the nudge is *subtracted* — the multi-leg form of ``_price``'s
    short-only arm.
    """
    credit = 0.0
    spread_total = 0.0
    for vertical in verticals:
        for contract, sign in ((vertical.short, 1.0), (vertical.long, -1.0)):
            assert contract.bid is not None and contract.ask is not None  # noqa: S101
            credit += sign * (contract.bid + contract.ask) / 2
            spread_total += contract.ask - contract.bid
    return credit - settings.limit_price_spread_nudge_pct * spread_total


def _assemble_verticals(
    intent: StrategyIntent,
    same_expiry: list[NormalizedContract],
    *,
    settings: Settings,
    spot: float,
    dte_days: int,
) -> list[_CreditVertical] | None:
    """The complete leg set for ``intent`` on one expiry, or ``None``.

    The width is resolved per expiry rather than once for the structure: it is
    sized from the expected move, and the expected move grows with the time
    left to run, so a near expiry legitimately wants tighter wings than a far
    one. Both sides of an iron share the one width, so its two wings stay
    symmetric and ``max_loss`` describes either side being breached.
    """
    increment = _strike_increment(same_expiry)
    if increment is None:
        return None
    width = _target_width(
        intent, same_expiry, settings=settings, spot=spot, dte_days=dte_days
    )
    if width is None or width <= 0:
        return None

    if intent.strategy == "put_credit_spread":
        put = _credit_vertical(
            same_expiry,
            "put",
            target_delta=intent.target_delta,
            width=width,
            increment=increment,
            settings=settings,
            spot=spot,
        )
        return [put] if put is not None else None

    if intent.strategy == "call_credit_spread":
        call = _credit_vertical(
            same_expiry,
            "call",
            target_delta=intent.target_delta,
            width=width,
            increment=increment,
            settings=settings,
            spot=spot,
        )
        return [call] if call is not None else None

    if intent.strategy == "iron_condor":
        put = _credit_vertical(
            same_expiry,
            "put",
            target_delta=intent.target_delta,
            width=width,
            increment=increment,
            settings=settings,
            spot=spot,
        )
        call = _credit_vertical(
            same_expiry,
            "call",
            target_delta=intent.target_delta,
            width=width,
            increment=increment,
            settings=settings,
            spot=spot,
        )
        if put is None or call is None:
            return None
        # The short strikes straddle spot; if they cross, this is not a condor.
        if put.short.strike >= call.short.strike:
            return None
        return [put, call]

    # iron_butterfly — both shorts on one at-the-money strike, wings either side.
    calls = {contract.strike for contract in same_expiry if contract.option_type == "call"}
    puts = {contract.strike for contract in same_expiry if contract.option_type == "put"}
    shared = sorted(calls & puts)
    if not shared:
        return None
    atm = min(shared, key=lambda strike: abs(strike - spot))
    put = _credit_vertical(
        same_expiry,
        "put",
        target_delta=intent.target_delta,
        width=width,
        increment=increment,
        settings=settings,
        spot=spot,
        short_strike=atm,
    )
    call = _credit_vertical(
        same_expiry,
        "call",
        target_delta=intent.target_delta,
        width=width,
        increment=increment,
        settings=settings,
        spot=spot,
        short_strike=atm,
    )
    if put is None or call is None:
        return None
    return [put, call]


def _build_credit_structure(
    intent: StrategyIntent,
    *,
    universe: list[NormalizedContract],
    account: dict[str, Any],
    settings: Settings,
    proposal_id: int,
    spot: float,
) -> OrderPlan | Rejection:
    """Assemble and price a defined-risk credit structure.

    Covers the four the Strategy Matrix reaches for whenever premium is
    expensive: both credit verticals and both iron structures. They share a
    shape — one or two short anchors, each with a protective wing on the same
    expiry — so they share a builder rather than four near-copies.
    """
    if intent.spread_width is None and _move_mult(intent.strategy, settings) <= 0:
        # Nothing pinned the width and nothing is allowed to derive one.
        return _reject(proposal_id, "missing_spread_width", strategy=intent.strategy)

    today = date.today()
    in_window = [
        contract
        for contract in universe
        if intent.dte_min <= (contract.expiry - today).days <= intent.dte_max
    ]
    if not in_window:
        return _reject(
            proposal_id,
            "no_contracts_in_window",
            underlying=intent.underlying,
            dte_min=intent.dte_min,
            dte_max=intent.dte_max,
        )

    # Every leg must share one expiry, so the expiry is chosen first: the one
    # whose complete leg set sits closest to the intent.
    best: tuple[float, list[_CreditVertical]] | None = None
    for expiry in sorted({contract.expiry for contract in in_window}):
        same_expiry = [contract for contract in in_window if contract.expiry == expiry]
        verticals = _assemble_verticals(
            intent,
            same_expiry,
            settings=settings,
            spot=spot,
            dte_days=(expiry - today).days,
        )
        if verticals is None:
            continue
        penalty = (
            0.0
            if not settings.standard_monthly_expiry_preference or _is_standard_monthly(expiry)
            else 1e-6
        )
        score = sum(vertical.score for vertical in verticals) + penalty
        if best is None or score < best[0]:
            best = (score, verticals)

    if best is None:
        return _reject(
            proposal_id,
            "no_credit_structure_available",
            underlying=intent.underlying,
            strategy=intent.strategy,
            spread_width=intent.spread_width,
        )
    _score, verticals = best

    for vertical in verticals:
        for contract in vertical.contracts():
            rejection = _liquidity_rejection(
                proposal_id,
                contract,
                max_spread_pct=settings.max_spread_pct,
                max_spread_abs=settings.max_spread_abs,
                min_oi=settings.min_open_interest,
            )
            if rejection is not None:
                return rejection

    credit = _net_credit(verticals, settings)
    if credit <= 0:
        return _reject(proposal_id, "not_a_credit", net_credit=credit)

    # Only one side of an iron structure can be breached, so the exposure is
    # the widest wing, not the sum of them.
    width = max(vertical.width for vertical in verticals)
    if credit >= width:
        return _reject(proposal_id, "credit_exceeds_width", net_credit=credit, width=width)
    if credit / width < settings.min_credit_width_pct:
        return _reject(
            proposal_id,
            "thin_credit",
            net_credit=credit,
            width=width,
            credit_width_pct=credit / width,
            floor=settings.min_credit_width_pct,
        )
    # The opposite failure, and the one that looks like a bargain: collecting
    # nearly the whole width leaves almost no profit zone, and the resulting
    # tiny max_loss is exactly what makes position sizing scale the trade up.
    if credit / width > settings.max_credit_width_pct:
        return _reject(
            proposal_id,
            "credit_too_rich",
            net_credit=credit,
            width=width,
            credit_width_pct=credit / width,
            ceiling=settings.max_credit_width_pct,
        )

    max_loss = (width - credit) * _CONTRACT_MULTIPLIER
    max_profit = credit * _CONTRACT_MULTIPLIER

    equity = finite_float(account.get("equity"))
    if equity is None:
        return _reject(proposal_id, "no_account_equity")
    max_premium_budget = settings.max_premium_pct_per_trade * equity
    qty = math.floor(max_premium_budget / max_loss)
    if qty <= 0:
        return _reject(
            proposal_id,
            "zero_quantity",
            max_premium_budget=max_premium_budget,
            max_loss_per_contract=max_loss,
        )

    legs: list[Leg] = []
    for vertical in verticals:
        legs.extend(vertical.legs())

    # A vertical has one breakeven and can state it; an iron has two, and one
    # field cannot hold both — reporting either alone reads as "the" breakeven.
    breakeven: float | None = None
    if intent.strategy == "put_credit_spread":
        breakeven = verticals[0].short.strike - credit
    elif intent.strategy == "call_credit_spread":
        breakeven = verticals[0].short.strike + credit

    return OrderPlan(
        proposal_id=proposal_id,
        underlying=intent.underlying,
        strategy=intent.strategy,
        legs=legs,
        qty=qty,
        # The one and only place the sign is inverted. Alpaca reads a negative
        # multi-leg limit price as a credit to be collected; everything above
        # carries the credit positive.
        limit_price=-credit,
        max_loss=max_loss * qty,
        max_profit=max_profit * qty,
        breakeven=breakeven,
        client_order_id=f"om-{proposal_id}",
    )


def _closest_to_delta(
    contracts: list[NormalizedContract],
    option_type: Literal["call", "put"],
    target_delta: float,
    settings: Settings,
    spot: float,
) -> tuple[NormalizedContract, float, float] | None:
    """The contract of ``option_type`` whose |delta| is nearest ``target_delta``."""
    best: tuple[NormalizedContract, float, float] | None = None
    for contract in contracts:
        if contract.option_type != option_type:
            continue
        effective = _effective_delta(contract, spot=spot, risk_free_rate=settings.risk_free_rate)
        if effective is None:
            continue
        delta, _source = effective
        diff = abs(abs(delta) - target_delta)
        if best is None or diff < best[2]:
            best = (contract, delta, diff)
    return best


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
    settings: Settings,
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
        # The realised distance between the two strikes, not the width the
        # intent asked for: the second leg snapped to a listed strike and the
        # two can differ by up to one increment. Every dollar figure below has
        # to describe the contracts actually being submitted.
        width = abs(primary.strike - secondary.strike)
        if entry_price >= width:
            return None, None, None, _reject(
                proposal_id, "net_debit_exceeds_width", net_debit=entry_price, width=width
            )
        if entry_price / width > settings.max_debit_width_pct:
            return None, None, None, _reject(
                proposal_id,
                "debit_exceeds_width_pct",
                net_debit=entry_price,
                width=width,
                debit_width_pct=entry_price / width,
                ceiling=settings.max_debit_width_pct,
            )
        max_loss = entry_price * mult
        max_profit = (width - entry_price) * mult
        if max_profit / max_loss < settings.min_reward_risk:
            return None, None, None, _reject(
                proposal_id,
                "reward_risk_too_low",
                max_profit=max_profit,
                max_loss=max_loss,
                reward_risk=max_profit / max_loss,
                floor=settings.min_reward_risk,
            )
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
