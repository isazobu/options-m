"""Tests for contract selection and pricing.

This is the anti-hallucination boundary: the caller states a structure and a
target delta, and this module picks the real contracts. The cases that matter
most are the refusals — a structure it cannot build must be refused by name,
never bent into a different one.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from options_m import strategy_builder
from options_m.config import Settings
from options_m.models import OrderPlan, Rejection, StrategyIntent

_EXPIRY = date.today() + timedelta(days=30)
_SPOT = 100.0


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"database_url": None}
    base.update(overrides)
    return Settings(**base)


def _intent(**overrides: Any) -> StrategyIntent:
    base: dict[str, Any] = {
        "action": "open",
        "strategy": "long_call",
        "underlying": "SPY",
        "target_delta": 0.25,
        "dte_min": 21,
        "dte_max": 38,
        "conviction": 0.8,
        "thesis": "test",
        "invalidation": "test",
    }
    base.update(overrides)
    return StrategyIntent(**base)


def _occ(strike: float, option_type: str) -> str:
    letter = "C" if option_type == "call" else "P"
    return f"SPY{_EXPIRY:%y%m%d}{letter}{int(strike * 1000):08d}"


def _contract(strike: float, option_type: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": _occ(strike, option_type),
        "type": option_type,
        "status": "active",
        "tradable": True,
        "strike_price": str(strike),
        "expiration_date": _EXPIRY.isoformat(),
        "open_interest": "500",
    }
    base.update(overrides)
    return base


def _snapshot(
    *, bid: float, ask: float, delta: float | None = None, iv: float | None = None
) -> dict[str, Any]:
    snap: dict[str, Any] = {"latestQuote": {"bp": bid, "ap": ask}}
    if delta is not None:
        snap["greeks"] = {"delta": delta}
    if iv is not None:
        snap["impliedVolatility"] = iv
    return snap


def _chain(
    strikes: list[float], *, with_greeks: bool = True, iv: float = 0.25
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """A small two-sided chain around ``_SPOT`` with tight, liquid quotes."""
    contracts: list[dict[str, Any]] = []
    snapshots: dict[str, dict[str, Any]] = {}
    for strike in strikes:
        for option_type in ("call", "put"):
            contract = _contract(strike, option_type)
            contracts.append(contract)
            # Rough but monotonic: further OTM is cheaper and lower delta.
            moneyness = (strike - _SPOT) / _SPOT
            price = max(0.6, 5.0 - abs(moneyness) * 30.0)
            delta = 0.5 - moneyness * 4 if option_type == "call" else -0.5 - moneyness * 4
            snapshots[contract["symbol"]] = _snapshot(
                bid=price - 0.05,
                ask=price + 0.05,
                delta=delta if with_greeks else None,
                iv=iv if with_greeks else None,
            )
    return contracts, snapshots


_ACCOUNT = {"equity": "100000", "cash": "100000"}


async def _build(intent: StrategyIntent, **overrides: Any) -> OrderPlan | Rejection:
    contracts, snapshots = overrides.pop("chain", None) or _chain([90.0, 95.0, 100.0, 105.0, 110.0])
    kwargs: dict[str, Any] = {
        "contracts": contracts,
        "snapshots": snapshots,
        "account": _ACCOUNT,
        "existing_position": None,
        "settings": _settings(),
        "proposal_id": 1,
        "spot": _SPOT,
    }
    kwargs.update(overrides)
    return await strategy_builder.build(intent, **kwargs)


# ---------------------------------------------------------------------------
# Refusals — the safety-critical half
# ---------------------------------------------------------------------------

async def test_a_structure_with_no_builder_is_refused_by_name() -> None:
    """A structure this module cannot assemble must be refused, never bent.

    Without the gate an unrecognised strategy is not in _CALL_STRATEGIES so it
    defaults to puts, is not a vertical so it grows no second leg, and falls
    through _risk_profile into the cash-secured-put arm — it would be
    submitted as a lone put priced as something else entirely.

    Every name StrategyIntent accepts now has a builder, so reaching the gate
    at all needs model_construct to bypass the Literal. That is the point: the
    guard has to hold for whatever the matrix learns to emit next.
    """
    intent = StrategyIntent.model_construct(
        action="open",
        strategy="covered_strangle",
        underlying="SPY",
        target_delta=0.25,
        spread_width=5.0,
        dte_min=21,
        dte_max=38,
        conviction=0.8,
        thesis="test",
        invalidation="test",
    )

    result = await _build(intent)

    assert isinstance(result, Rejection), "an unbuildable structure must not build"
    assert result.reason == "unsupported_strategy"
    assert result.detail["strategy"] == "covered_strangle"


async def test_the_matrix_spelling_of_a_debit_vertical_builds() -> None:
    """matrix.py says call_debit_spread; this module grew up on debit_call_spread."""
    result = await _build(_intent(strategy="call_debit_spread", spread_width=5.0))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert result.strategy == "debit_call_spread"
    assert len(result.legs) == 2
    assert [leg.side for leg in result.legs] == ["buy", "sell"]


async def test_an_empty_dte_window_is_refused_not_widened() -> None:
    result = await _build(_intent(dte_min=200, dte_max=300))

    assert isinstance(result, Rejection)
    assert result.reason == "no_contracts_in_window"


# ---------------------------------------------------------------------------
# Implied-vol fallback
# ---------------------------------------------------------------------------

def test_iv_is_solved_from_the_mid_when_the_snapshot_carries_none() -> None:
    """Alpaca's paper feed returns greeks: None — without this nothing scores."""
    contracts, snapshots = _chain([100.0], with_greeks=False)

    without_spot = strategy_builder.normalize_contracts(contracts, snapshots)
    with_spot = strategy_builder.normalize_contracts(contracts, snapshots, spot=_SPOT)

    assert all(c.implied_volatility is None for c in without_spot)
    assert all(c.implied_volatility is not None and c.implied_volatility > 0 for c in with_spot)


async def test_a_plan_still_builds_when_the_chain_has_no_greeks() -> None:
    chain = _chain([90.0, 95.0, 100.0, 105.0, 110.0], with_greeks=False)

    result = await _build(_intent(strategy="long_call"), chain=chain)

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert result.legs[0].delta_source == "black_scholes"


# ---------------------------------------------------------------------------
# Long strangle
# ---------------------------------------------------------------------------

async def test_a_long_strangle_buys_a_call_and_a_put_on_one_expiry() -> None:
    result = await _build(_intent(strategy="long_strangle"))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert len(result.legs) == 2
    assert {leg.option_type for leg in result.legs} == {"call", "put"}
    assert all(leg.side == "buy" for leg in result.legs)
    assert len({leg.expiry for leg in result.legs}) == 1


async def test_a_strangle_keeps_the_call_strike_above_the_put_strike() -> None:
    """Equal strikes would be a straddle — a different structure and risk."""
    result = await _build(_intent(strategy="long_strangle"))

    assert isinstance(result, OrderPlan)
    call = next(leg for leg in result.legs if leg.option_type == "call")
    put = next(leg for leg in result.legs if leg.option_type == "put")
    assert call.strike > put.strike


async def test_a_strangles_max_loss_is_the_debit_and_its_profit_is_unbounded() -> None:
    result = await _build(_intent(strategy="long_strangle"))

    assert isinstance(result, OrderPlan)
    assert result.max_loss == result.limit_price * 100 * result.qty
    assert result.max_profit is None
    # Two breakevens cannot honestly be reported in one field.
    assert result.breakeven is None


# ---------------------------------------------------------------------------
# Credit structures
#
# On the default chain (strikes 90/95/100/105/110, mids 2.0/3.5/5.0/3.5/2.0,
# call deltas .9/.7/.5/.3/.1) a 0.25 target picks the 105 call and the 95 put.
# Note the price curve peaks at 100, so a vertical straddling spot nets zero —
# credit verticals must be built on one side, which is what they do.
# ---------------------------------------------------------------------------

def _credit_intent(strategy: str, **overrides: Any) -> StrategyIntent:
    overrides.setdefault("spread_width", 5.0)
    return _intent(strategy=strategy, **overrides)


async def test_a_put_credit_spread_sells_the_anchor_and_buys_the_wing_below() -> None:
    result = await _build(_credit_intent("put_credit_spread"))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert len(result.legs) == 2
    assert {leg.option_type for leg in result.legs} == {"put"}
    short = next(leg for leg in result.legs if leg.side == "sell")
    long = next(leg for leg in result.legs if leg.side == "buy")
    # The protective wing is further out of the money than what it protects.
    assert long.strike < short.strike
    assert len({leg.expiry for leg in result.legs}) == 1


async def test_a_call_credit_spread_buys_the_wing_above() -> None:
    result = await _build(_credit_intent("call_credit_spread"))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert {leg.option_type for leg in result.legs} == {"call"}
    short = next(leg for leg in result.legs if leg.side == "sell")
    long = next(leg for leg in result.legs if leg.side == "buy")
    assert long.strike > short.strike


async def test_an_iron_condor_is_two_credit_verticals_on_one_expiry() -> None:
    result = await _build(_credit_intent("iron_condor"))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert len(result.legs) == 4
    assert len({leg.expiry for leg in result.legs}) == 1
    shorts = [leg for leg in result.legs if leg.side == "sell"]
    longs = [leg for leg in result.legs if leg.side == "buy"]
    assert {leg.option_type for leg in shorts} == {"call", "put"}
    assert {leg.option_type for leg in longs} == {"call", "put"}
    short_put = next(leg for leg in shorts if leg.option_type == "put")
    short_call = next(leg for leg in shorts if leg.option_type == "call")
    # The two shorts straddle spot; crossing them would not be a condor.
    assert short_put.strike < short_call.strike


async def test_an_iron_butterfly_puts_both_shorts_on_one_strike() -> None:
    result = await _build(_credit_intent("iron_butterfly"))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert len(result.legs) == 4
    shorts = [leg for leg in result.legs if leg.side == "sell"]
    assert len({leg.strike for leg in shorts}) == 1
    # ...and that strike is the one nearest spot, not a delta-selected one.
    assert next(iter({leg.strike for leg in shorts})) == _SPOT


async def test_every_short_leg_is_paired_with_a_wing_of_its_own_type() -> None:
    """Alpaca rejects a naked short inside a multi-leg order, and so do we."""
    for strategy in ("put_credit_spread", "call_credit_spread", "iron_condor", "iron_butterfly"):
        result = await _build(_credit_intent(strategy))
        assert isinstance(result, OrderPlan), getattr(result, "reason", None)
        for option_type in ("call", "put"):
            shorts = [
                leg
                for leg in result.legs
                if leg.side == "sell" and leg.option_type == option_type
            ]
            longs = [
                leg for leg in result.legs if leg.side == "buy" and leg.option_type == option_type
            ]
            assert len(longs) >= len(shorts), f"{strategy}: naked {option_type} short"


async def test_a_credit_structure_submits_a_negative_limit_price() -> None:
    """Alpaca reads a negative multi-leg limit price as a credit collected.

    The sign is inverted in exactly one place, when the plan is built. Getting
    it backwards submits a credit spread as if paying a debit for it.
    """
    for strategy in ("put_credit_spread", "call_credit_spread", "iron_condor", "iron_butterfly"):
        result = await _build(_credit_intent(strategy))
        assert isinstance(result, OrderPlan), getattr(result, "reason", None)
        assert result.limit_price < 0, f"{strategy} must submit a credit"


async def test_a_debit_structure_still_submits_a_positive_limit_price() -> None:
    """The mirror of the credit case — one assertion per family, not per name."""
    result = await _build(_intent(strategy="call_debit_spread", spread_width=5.0))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert result.limit_price > 0


async def test_a_credit_structures_max_loss_is_the_width_less_the_credit() -> None:
    result = await _build(_credit_intent("put_credit_spread"))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    short = next(leg for leg in result.legs if leg.side == "sell")
    long = next(leg for leg in result.legs if leg.side == "buy")
    width = abs(short.strike - long.strike)
    credit = abs(result.limit_price)

    assert result.max_loss == (width - credit) * 100 * result.qty
    assert result.max_profit == credit * 100 * result.qty
    # A vertical has exactly one breakeven and can state it honestly.
    assert result.breakeven == short.strike - credit


async def test_an_irons_max_loss_uses_the_widest_wing_not_the_sum() -> None:
    """Only one side of an iron can be breached, so only one width is at risk."""
    result = await _build(_credit_intent("iron_condor"))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    shorts = {leg.option_type: leg for leg in result.legs if leg.side == "sell"}
    longs = {leg.option_type: leg for leg in result.legs if leg.side == "buy"}
    widths = [abs(shorts[t].strike - longs[t].strike) for t in ("call", "put")]
    credit = abs(result.limit_price)

    assert result.max_loss == (max(widths) - credit) * 100 * result.qty
    # Two breakevens cannot honestly be reported in one field.
    assert result.breakeven is None


async def test_a_credit_thinner_than_the_floor_is_refused() -> None:
    """A 12%-of-width floor: below it the premium does not pay for the risk."""
    result = await _build(
        _credit_intent("put_credit_spread"), settings=_settings(min_credit_width_pct=0.95)
    )

    assert isinstance(result, Rejection)
    assert result.reason == "thin_credit"
    assert result.detail["floor"] == 0.95


async def test_a_credit_richer_than_the_ceiling_is_refused() -> None:
    """The mirror of thin_credit, and the one that looks like a bargain.

    Credit/width is roughly the chance of being breached, so collecting most
    of the width leaves almost no profit zone — and the tiny max loss that
    results is exactly what makes position sizing scale the trade up. Measured
    on a real SPY chain, a 5-wide ATM iron butterfly collected 95.7% of width
    and sized to 91 contracts.
    """
    result = await _build(
        _credit_intent("put_credit_spread"), settings=_settings(max_credit_width_pct=0.05)
    )

    assert isinstance(result, Rejection)
    assert result.reason == "credit_too_rich"
    assert result.detail["ceiling"] == 0.05


async def test_the_credit_band_accepts_what_sits_between_its_edges() -> None:
    """Floor and ceiling must not be so close together that nothing survives."""
    result = await _build(_credit_intent("iron_condor"))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    settings = _settings()
    credit_width_pct = abs(result.limit_price) / 5.0
    assert settings.min_credit_width_pct <= credit_width_pct <= settings.max_credit_width_pct


async def test_a_wing_further_than_one_strike_increment_is_refused() -> None:
    """Snapping a wing to a far strike would build a different structure."""
    result = await _build(_credit_intent("put_credit_spread", spread_width=40.0))

    assert isinstance(result, Rejection)
    assert result.reason == "no_credit_structure_available"


async def test_a_credit_structure_that_cannot_be_sized_is_refused_not_silent() -> None:
    result = await _build(
        _credit_intent("iron_condor"), account={"equity": "100", "cash": "100"}
    )

    assert isinstance(result, Rejection)
    assert result.reason == "zero_quantity"


# ---------------------------------------------------------------------------
# Wing width scaling
#
# A flat dollar width is only ever right for one underlying at one vol level.
# The wings are sized from the expected move over the option's life instead,
# so the same settings work on a $30 name and on SPY at 769.
# ---------------------------------------------------------------------------

def _wide_chain(
    *, iv: float = 0.25
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """A dense $1 ladder that stays liquid all the way out to the wings.

    The default ``_chain`` floors its prices at 0.60, so a wing pushed far
    enough out is refused on liquidity before the width can be compared —
    which is the wrong thing to be measuring here. Every quote in this one is
    two cents wide, so only the width varies.

    Delta is clamped and the put derived from the call by parity, so it stays
    monotone across the whole ladder. ``_chain``'s bare linear form is fine
    over its five near-the-money strikes but turns a deep put delta positive
    out here, which would hand the short leg to the wrong end of the chain.
    """
    contracts: list[dict[str, Any]] = []
    snapshots: dict[str, dict[str, Any]] = {}
    for i in range(41):
        strike = 80.0 + i
        moneyness = (strike - _SPOT) / _SPOT
        price = max(1.0, 5.0 - abs(moneyness) * 20.0)
        call_delta = max(0.01, min(0.99, 0.5 - moneyness * 4))
        for option_type in ("call", "put"):
            contract = _contract(strike, option_type)
            contracts.append(contract)
            snapshots[contract["symbol"]] = _snapshot(
                bid=price - 0.02,
                ask=price + 0.02,
                delta=call_delta if option_type == "call" else call_delta - 1.0,
                iv=iv,
            )
    return contracts, snapshots


def _width_of(plan: OrderPlan) -> float:
    shorts = {leg.option_type: leg for leg in plan.legs if leg.side == "sell"}
    longs = {leg.option_type: leg for leg in plan.legs if leg.side == "buy"}
    return max(abs(shorts[t].strike - longs[t].strike) for t in shorts)


async def test_wings_widen_when_implied_vol_rises() -> None:
    """Same strikes, same deltas, more vol — a wider expected move to cover."""
    calm = await _build(_intent(strategy="put_credit_spread"), chain=_wide_chain(iv=0.15))
    stormy = await _build(_intent(strategy="put_credit_spread"), chain=_wide_chain(iv=0.60))

    assert isinstance(calm, OrderPlan), getattr(calm, "reason", None)
    assert isinstance(stormy, OrderPlan), getattr(stormy, "reason", None)
    assert _width_of(stormy) > _width_of(calm)


async def test_an_explicit_width_is_honoured_exactly() -> None:
    """The CLI's --spread-width pins the structure; scaling must not override it."""
    result = await _build(
        _intent(strategy="put_credit_spread", spread_width=5.0), chain=_wide_chain()
    )

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert _width_of(result) == 5.0


async def test_an_at_the_money_structure_gets_wider_wings_than_a_delta_selected_one() -> None:
    """An ATM short collects far more premium, so it needs more room.

    Sized on the condor's multiplier an iron butterfly collects almost its
    whole width and has no profit zone left — measured at 95.7% on a real SPY
    chain. The two families cannot share one setting.
    """
    condor = await _build(_intent(strategy="iron_condor"), chain=_wide_chain())
    butterfly = await _build(_intent(strategy="iron_butterfly"), chain=_wide_chain())

    assert isinstance(condor, OrderPlan), getattr(condor, "reason", None)
    assert isinstance(butterfly, OrderPlan), getattr(butterfly, "reason", None)
    assert _width_of(butterfly) > _width_of(condor)


async def test_scaling_off_and_no_width_is_refused_not_guessed() -> None:
    result = await _build(
        _intent(strategy="put_credit_spread"),
        chain=_wide_chain(),
        settings=_settings(
            spread_width_expected_move_mult=0.0, spread_width_expected_move_mult_atm=0.0
        ),
    )

    assert isinstance(result, Rejection)
    assert result.reason == "missing_spread_width"


# ---------------------------------------------------------------------------
# Liquidity
# ---------------------------------------------------------------------------

async def test_a_cheap_wing_is_not_refused_for_being_wide_in_percentage_alone() -> None:
    """A 0.10/0.15 wing is 40% wide but five cents to cross.

    Refusing it on the percentage alone would forbid every iron structure,
    since the cheap far wing is what makes the structure defined-risk.
    """
    contracts, snapshots = _chain([90.0, 95.0, 100.0, 105.0, 110.0])
    wing = _occ(90.0, "put")
    snapshots[wing] = _snapshot(bid=0.10, ask=0.15, delta=-0.10, iv=0.25)

    result = await _build(_credit_intent("put_credit_spread"), chain=(contracts, snapshots))

    assert isinstance(result, OrderPlan), getattr(result, "reason", None)
    assert any(leg.symbol == wing for leg in result.legs)


async def test_a_leg_wide_in_both_percentage_and_cash_is_still_refused() -> None:
    contracts, snapshots = _chain([90.0, 95.0, 100.0, 105.0, 110.0])
    wing = _occ(90.0, "put")
    snapshots[wing] = _snapshot(bid=1.00, ask=1.60, delta=-0.10, iv=0.25)

    result = await _build(_credit_intent("put_credit_spread"), chain=(contracts, snapshots))

    assert isinstance(result, Rejection)
    assert result.reason == "wide_spread"
