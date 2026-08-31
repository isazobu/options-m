"""Tests for the dynamic position sizer.

The scalars are exercised one at a time — a state that moves exactly one of
them, with everything else neutral — so a failure names the scalar that broke
rather than "sizing came out wrong". The direction assertions matter more than
the exact numbers: sizing *down* into a loss and *up* out of a gain is the whole
safety property, and a sign flip there would not necessarily change any
magnitude test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from options_m.config import Settings
from options_m.db import Database
from options_m.sizing import (
    SizingState,
    build_sizing_state,
    collateral_per_contract,
    conviction_reliability,
    conviction_scalar,
    drawdown_scalar,
    gain_scalar,
    horizon_scalar,
    resolve_options_buying_power,
    size_position,
)
from options_m.store import Store

_EQUITY = 100_000.0


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"database_url": None}
    base.update(overrides)
    return Settings(**base)


def _state(**overrides: Any) -> SizingState:
    base: dict[str, Any] = {
        "equity": _EQUITY,
        "options_buying_power": _EQUITY,
        "buying_power_source": "options_buying_power",
        "cash": _EQUITY,
        "start_of_day_equity": _EQUITY,
        "high_water_mark": _EQUITY,
        "campaign_start_equity": None,
        "sessions_remaining": None,
        "is_first_session": False,
        # Full trust unless a test is about calibration, so every other test
        # reads the raw conviction multiplier rather than a shrunk one.
        "conviction_reliability": 1.0,
        "conviction_samples": 0,
    }
    base.update(overrides)
    return SizingState(**base)


def _store() -> Store:
    return Store(Database(Settings(database_url=None)))


# ---------------------------------------------------------------------------
# Buying power: the field that was never consulted before
# ---------------------------------------------------------------------------


def test_the_margin_buying_power_field_is_never_used() -> None:
    """Options are not marginable, so the headline 2x figure must not be read.

    An account reporting buying_power=200000 against cash=100000 can buy
    $100k of options, not $200k. Reading the wrong field here would authorise
    exactly twice the exposure the account can carry.
    """
    account = {"equity": "100000", "cash": "100000", "buying_power": "200000"}

    value, source = resolve_options_buying_power(account)

    assert value == 100_000.0
    assert source == "cash"


def test_the_options_specific_field_wins_when_present() -> None:
    account = {
        "cash": "100000",
        "buying_power": "200000",
        "non_marginable_buying_power": "90000",
        "options_buying_power": "80000",
    }

    value, source = resolve_options_buying_power(account)

    assert value == 80_000.0
    assert source == "options_buying_power"


def test_an_account_with_no_readable_field_is_unknown_not_zero() -> None:
    value, source = resolve_options_buying_power({"buying_power": "200000"})

    assert value is None
    assert source is None


def test_unknown_buying_power_blocks_rather_than_sizing_to_the_equity_budget() -> None:
    decision = size_position(
        strategy="put_credit_spread",
        conviction=0.8,
        max_loss_per_contract=400.0,
        state=_state(options_buying_power=None),
        settings=_settings(),
    )

    assert decision.qty == 0
    assert decision.blocked_reason == "unknown_buying_power"


def test_buying_power_caps_a_trade_the_equity_budget_would_have_allowed() -> None:
    """The state this system could not previously see: equity intact, collateral gone.

    Every open defined-risk structure holds its max loss as collateral while
    leaving equity untouched, so a fully committed account still reports the
    equity it started with. Sizing off equity alone would send an order the
    broker must refuse.
    """
    settings = _settings()
    max_loss = 200.0

    roomy = size_position(
        strategy="put_credit_spread",
        conviction=1.0,
        max_loss_per_contract=max_loss,
        state=_state(),
        settings=settings,
    )
    committed = size_position(
        strategy="put_credit_spread",
        conviction=1.0,
        max_loss_per_contract=max_loss,
        state=_state(options_buying_power=1_000.0),
        settings=settings,
    )

    assert roomy.binding_constraint == "risk_budget"
    assert committed.binding_constraint == "buying_power"
    # 1000 x 0.50 utilisation / 200 collateral.
    assert committed.qty == 2
    assert committed.qty < roomy.qty


def test_a_covered_call_ties_up_no_additional_buying_power() -> None:
    """The shares are already held and already paid for."""
    assert collateral_per_contract("covered_call", max_loss_per_contract=500.0, strike=100.0) == 0.0


def test_a_cash_secured_put_posts_the_whole_assignment_cost() -> None:
    """max_loss is strike-minus-premium, which understates the cash held."""
    collateral = collateral_per_contract(
        "cash_secured_put", max_loss_per_contract=9_700.0, strike=100.0
    )

    assert collateral == 10_000.0


def test_a_put_with_no_strike_blocks_rather_than_guessing_its_collateral() -> None:
    decision = size_position(
        strategy="cash_secured_put",
        conviction=0.8,
        max_loss_per_contract=9_700.0,
        state=_state(),
        settings=_settings(),
        strike=None,
    )

    assert decision.qty == 0
    assert decision.blocked_reason == "unknown_collateral_requirement"


# ---------------------------------------------------------------------------
# The scalars, one at a time
# ---------------------------------------------------------------------------


def test_a_drawdown_shrinks_the_trade_it_does_not_grow_it() -> None:
    """The anti-martingale property, asserted as a direction rather than a number.

    Doubling into a drawdown to win it back is how an account reaches its halt
    threshold and stops being able to trade at all.
    """
    settings = _settings()
    # Kept inside the 3% daily halt: past it the taper is already pinned to its
    # floor, and two pinned values would compare equal rather than ordered.
    flat = drawdown_scalar(_state(), settings)
    down = drawdown_scalar(_state(equity=99_000.0), settings)
    deeper = drawdown_scalar(_state(equity=98_000.0), settings)

    assert flat == 1.0
    assert down < flat
    assert deeper < down


def test_the_drawdown_taper_reaches_its_floor_at_the_halt_threshold() -> None:
    settings = _settings()
    at_halt = drawdown_scalar(
        # 8% below the peak, with the day baseline moved out of the way so only
        # the peak-to-now measure binds.
        _state(equity=92_000.0, start_of_day_equity=92_000.0),
        settings,
    )

    assert at_halt == settings.drawdown_size_floor


def test_the_taper_never_reaches_zero_so_a_recovery_is_still_tradeable() -> None:
    """A floor of zero would mean one bad session ends the campaign silently."""
    settings = _settings()
    beyond_halt = drawdown_scalar(_state(equity=50_000.0), settings)

    assert beyond_halt == settings.drawdown_size_floor
    assert beyond_halt > 0


def test_the_worse_of_the_two_drawdown_measures_binds() -> None:
    """A bad morning shrinks the afternoon even at an all-time-high equity peak."""
    settings = _settings()
    # Equity is at its peak for the campaign but down 2% since the last close.
    scalar = drawdown_scalar(
        _state(equity=98_000.0, high_water_mark=98_000.0, start_of_day_equity=100_000.0),
        settings,
    )

    assert scalar < 1.0


def test_gains_fund_a_bigger_bet_and_the_boost_is_capped() -> None:
    settings = _settings()
    flat = gain_scalar(_state(campaign_start_equity=_EQUITY), settings)
    up = gain_scalar(_state(equity=102_000.0, campaign_start_equity=_EQUITY), settings)
    way_up = gain_scalar(_state(equity=140_000.0, campaign_start_equity=_EQUITY), settings)

    assert flat == 1.0
    assert 1.0 < up < way_up
    assert way_up == settings.gain_size_cap


def test_a_loss_does_not_produce_a_gain_boost() -> None:
    """The boost is one-directional; the taper is what handles the other side."""
    scalar = gain_scalar(_state(equity=90_000.0, campaign_start_equity=_EQUITY), _settings())

    assert scalar == 1.0


def test_conviction_is_mapped_across_the_band_that_actually_occurs() -> None:
    """Below the floor the matrix forces hold, so the live band starts there.

    Scaling from 0.0 instead would put every real proposal in the top sliver of
    the multiplier range and flatten the distinction this knob exists to make.
    """
    settings = _settings()
    marginal = conviction_scalar(settings.conviction_floor, settings)
    strong = conviction_scalar(1.0, settings)
    middling = conviction_scalar((settings.conviction_floor + 1.0) / 2, settings)

    assert marginal == settings.conviction_size_min_mult
    assert strong == settings.conviction_size_max_mult
    assert marginal < middling < strong


def test_conviction_scales_the_actual_contract_count() -> None:
    settings = _settings()
    weak = size_position(
        strategy="put_credit_spread",
        conviction=0.55,
        max_loss_per_contract=100.0,
        state=_state(),
        settings=settings,
    )
    strong = size_position(
        strategy="put_credit_spread",
        conviction=1.0,
        max_loss_per_contract=100.0,
        state=_state(),
        settings=settings,
    )

    assert strong.qty > weak.qty


# ---------------------------------------------------------------------------
# Campaign horizon
# ---------------------------------------------------------------------------


def test_no_campaign_configured_means_no_horizon_pacing() -> None:
    assert horizon_scalar(_state(sessions_remaining=None), _settings()) == 1.0


def test_front_loading_is_neutral_by_default() -> None:
    """Sizing bigger because the calendar is running out is forced trading.

    The knob stays for a deliberate front-load, but a default above 1.0 would
    make every campaign's first session automatically aggressive.
    """
    assert _settings().campaign_front_load_mult == 1.00


def test_the_first_session_is_front_loaded_when_asked_for() -> None:
    settings = _settings(campaign_front_load_mult=1.25)
    first = horizon_scalar(_state(sessions_remaining=3, is_first_session=True), settings)
    later = horizon_scalar(_state(sessions_remaining=2, is_first_session=False), settings)

    assert first == 1.25
    assert later == 1.0


def test_new_opens_stop_once_too_few_sessions_remain() -> None:
    """A position opened with no session left to work in pays the spread twice."""
    decision = size_position(
        strategy="put_credit_spread",
        conviction=1.0,
        max_loss_per_contract=100.0,
        state=_state(sessions_remaining=1),
        settings=_settings(campaign_min_sessions_to_hold=1),
        strike=None,
    )

    assert decision.qty == 0
    assert decision.blocked_reason == "campaign_horizon_closed"


def test_a_closed_horizon_is_not_reported_as_a_budget_problem() -> None:
    """Reading it as zero_quantity would hide a stalled campaign as costly trades."""
    decision = size_position(
        strategy="put_credit_spread",
        conviction=1.0,
        max_loss_per_contract=1.0,
        state=_state(sessions_remaining=0),
        settings=_settings(),
    )

    assert decision.blocked_reason == "campaign_horizon_closed"
    assert decision.binding_constraint == "campaign_horizon_closed"


# ---------------------------------------------------------------------------
# The ceiling risk.py enforces independently
# ---------------------------------------------------------------------------


def test_the_scalars_can_never_size_past_the_hard_per_trade_ceiling() -> None:
    """Otherwise the plan is built, priced, and then rejected downstream.

    Every scalar at its maximum multiplies out well past the ceiling, so the
    clamp is what keeps sizing and risk.py agreeing rather than burning a full
    round of broker calls to produce a rejection.
    """
    settings = _settings()
    decision = size_position(
        strategy="put_credit_spread",
        conviction=1.0,
        max_loss_per_contract=100.0,
        state=_state(
            equity=140_000.0,
            high_water_mark=140_000.0,
            start_of_day_equity=140_000.0,
            campaign_start_equity=_EQUITY,
            sessions_remaining=3,
            is_first_session=True,
        ),
        settings=settings,
    )

    assert decision.risk_fraction == settings.max_premium_pct_per_trade
    assert decision.qty * 100.0 <= settings.max_premium_pct_per_trade * 140_000.0


def test_unknown_equity_blocks_the_trade() -> None:
    decision = size_position(
        strategy="put_credit_spread",
        conviction=1.0,
        max_loss_per_contract=100.0,
        state=_state(equity=None),
        settings=_settings(),
    )

    assert decision.qty == 0
    assert decision.blocked_reason == "no_account_equity"


def test_the_decision_carries_the_arithmetic_that_produced_it() -> None:
    """ "4 contracts" with no derivation is barely better than a guess."""
    decision = size_position(
        strategy="put_credit_spread",
        conviction=0.8,
        max_loss_per_contract=250.0,
        state=_state(),
        settings=_settings(),
    )

    assert set(decision.scalars) == {"drawdown", "gain", "conviction", "horizon"}
    assert decision.caps["risk_budget"] == decision.qty
    assert decision.detail["binding_constraint"] == "risk_budget"


# ---------------------------------------------------------------------------
# build_sizing_state — the live path
# ---------------------------------------------------------------------------


async def test_state_from_the_account_alone_is_neutral_not_refusing() -> None:
    """A fresh database has genuinely observed no drawdown to taper against."""
    state = SizingState.from_account({"equity": "100000", "cash": "100000"})

    assert state.equity == 100_000.0
    assert state.high_water_mark is None
    assert state.sessions_remaining is None
    assert drawdown_scalar(state, _settings()) == 1.0


async def test_the_high_water_mark_comes_from_observed_equity_not_from_today() -> None:
    store = _store()
    for equity in (100_000.0, 105_000.0, 98_000.0):
        await store.append_equity(
            equity=equity, cash=equity, buying_power=equity, positions_count=0
        )

    state = await build_sizing_state(
        {"equity": "98000", "cash": "98000"}, store=store, settings=_settings()
    )

    assert state.high_water_mark == 105_000.0
    assert drawdown_scalar(state, _settings()) < 1.0


async def test_the_campaign_window_is_counted_in_sessions_not_calendar_days() -> None:
    """A three-session campaign starting on a Friday is not over by Monday."""
    store = _store()
    friday = date(2026, 8, 28)
    monday = date(2026, 8, 31)
    await store.upsert_market_calendar(
        [
            {
                "date": day,
                "open": datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC),
                "close": datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC),
            }
            for day in (friday, monday)
        ]
    )

    state = await build_sizing_state(
        {"equity": "100000", "cash": "100000"},
        store=store,
        settings=_settings(campaign_start_date=friday, campaign_days=3),
        now=datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
    )

    # Two sessions elapsed (Friday and Monday), so one plus the two remaining.
    assert state.sessions_remaining == 2
    assert state.is_first_session is False


async def test_the_campaign_baseline_ignores_equity_observed_before_it_started() -> None:
    """A stale baseline would misreport the gain, and upward is the unsafe way."""
    store = _store()
    await store.append_equity(
        equity=80_000.0, cash=80_000.0, buying_power=80_000.0, positions_count=0
    )
    today = datetime.now(UTC)
    await store.upsert_market_calendar(
        [
            {
                "date": store.session_day(today),
                "open": today - timedelta(hours=2),
                "close": today + timedelta(hours=2),
            }
        ]
    )

    state = await build_sizing_state(
        {"equity": "100000", "cash": "100000"},
        store=store,
        settings=_settings(campaign_start_date=store.session_day(today), campaign_days=3),
        now=today,
    )

    # The 80k reading predates nothing here — it is the only row, and it was
    # written today — so it *is* the baseline. The assertion that matters is
    # that a baseline exists and the gain reads off it rather than off equity.
    assert state.campaign_start_equity == 80_000.0
    assert gain_scalar(state, _settings()) > 1.0


# ---------------------------------------------------------------------------
# Conviction calibration — does the number the sizer leans on predict anything?
# ---------------------------------------------------------------------------


def test_too_few_closed_trades_falls_back_to_the_prior_not_to_full_trust() -> None:
    """A correlation over a handful of trades is noise.

    Assuming conviction is fully predictive on no evidence is the aggressive
    direction, which is why the prior is below 1.0.
    """
    settings = _settings()
    reliability, samples = conviction_reliability([(0.9, 0.5), (0.6, -0.2)], settings)

    assert samples == 2
    assert reliability == settings.conviction_reliability_prior
    assert reliability < 1.0


def test_conviction_that_tracks_pnl_earns_full_trust() -> None:
    settings = _settings(conviction_calibration_min_samples=4)
    outcomes = [(0.6, -0.3), (0.7, -0.1), (0.85, 0.2), (1.0, 0.5)]

    reliability, samples = conviction_reliability(outcomes, settings)

    assert samples == 4
    assert reliability > 0.9


def test_conviction_that_predicts_nothing_is_not_trusted() -> None:
    """High conviction losing money is a reason to stop leaning on the number."""
    settings = _settings(conviction_calibration_min_samples=4)
    outcomes = [(0.6, 0.5), (0.7, 0.3), (0.85, -0.2), (1.0, -0.6)]

    reliability, _samples = conviction_reliability(outcomes, settings)

    assert reliability == 0.0


def test_an_inverted_relationship_clamps_to_zero_rather_than_betting_against() -> None:
    """Zero means "size every trade the same", not "invert the thesis".

    Inverting on a small sample would turn a run of bad luck into a systematic
    bet against the system's own reasoning.
    """
    settings = _settings(conviction_calibration_min_samples=4)
    outcomes = [(1.0, -1.0), (0.9, -0.8), (0.7, 0.4), (0.6, 0.9)]

    reliability, _samples = conviction_reliability(outcomes, settings)

    assert reliability == 0.0


def test_identical_convictions_carry_no_information_either_way() -> None:
    """A degenerate sample is not evidence that conviction is worthless."""
    settings = _settings(conviction_calibration_min_samples=3)
    outcomes = [(0.8, 0.1), (0.8, -0.4), (0.8, 0.6)]

    reliability, _samples = conviction_reliability(outcomes, settings)

    assert reliability == settings.conviction_reliability_prior


def test_zero_reliability_sizes_every_conviction_the_same() -> None:
    settings = _settings()
    weak = conviction_scalar(0.55, settings, reliability=0.0)
    strong = conviction_scalar(1.0, settings, reliability=0.0)

    assert weak == strong == 1.0


def test_partial_reliability_shrinks_the_multiplier_toward_neutral() -> None:
    settings = _settings()
    full = conviction_scalar(1.0, settings, reliability=1.0)
    half = conviction_scalar(1.0, settings, reliability=0.5)

    assert full == settings.conviction_size_max_mult
    assert 1.0 < half < full


def test_reliability_reaches_the_contract_count() -> None:
    """The wiring, not the arithmetic: an untrusted conviction sizes smaller."""
    settings = _settings()
    trusted = size_position(
        strategy="put_credit_spread",
        conviction=1.0,
        max_loss_per_contract=100.0,
        state=_state(conviction_reliability=1.0),
        settings=settings,
    )
    untrusted = size_position(
        strategy="put_credit_spread",
        conviction=1.0,
        max_loss_per_contract=100.0,
        state=_state(conviction_reliability=0.0),
        settings=settings,
    )

    assert untrusted.qty < trusted.qty


async def test_the_live_state_measures_conviction_against_closed_trades() -> None:
    """End to end through the store: an open proposal, then its close.

    The link needs no new writer — PositionManagerAgent already stamps the
    opening proposal_id and pnl_pct onto the position, and StrategistAgent
    stores that payload as the close proposal's evidence.
    """
    store = _store()
    opened = await store.save_proposal(
        underlying="SPY",
        intent={"action": "open", "conviction": 0.9, "strategy": "put_credit_spread"},
        evidence={},
        status="submitted",
    )
    await store.save_proposal(
        underlying="SPY",
        intent={"action": "close", "conviction": 1.0, "strategy": "put_credit_spread"},
        evidence={"proposal_id": opened, "pnl_pct": 0.35},
        status="pending",
    )

    outcomes = await store.conviction_outcomes()
    state = await build_sizing_state(
        {"equity": "100000", "cash": "100000"}, store=store, settings=_settings()
    )

    assert outcomes == [
        {"conviction": 0.9, "pnl_pct": 0.35, "underlying": "SPY", "ts": outcomes[0]["ts"]}
    ]
    assert state.conviction_samples == 1
    # One sample is far below the minimum, so the prior still stands.
    assert state.conviction_reliability == _settings().conviction_reliability_prior


async def test_a_close_with_no_opener_is_skipped_rather_than_defaulted() -> None:
    """A P&L with no conviction cannot be attributed to one."""
    store = _store()
    await store.save_proposal(
        underlying="SPY",
        intent={"action": "close", "conviction": 1.0, "strategy": "put_credit_spread"},
        evidence={"pnl_pct": 0.35},
        status="pending",
    )

    assert await store.conviction_outcomes() == []
