"""StrategistAgent candidate selection.

The strategist runs every few minutes for a whole session. Under DRY_RUN a
name never becomes an open position, so without a cooldown and hard caps the
top-scored symbol is re-proposed — a fresh LLM call and a near-duplicate
proposal — on every single tick (review finding H2).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from options_m.agents.strategist import (
    StrategistAgent,
)
from options_m.config import Settings
from options_m.db import Database
from options_m.exits import close_reason as _close_reason
from options_m.exits import exit_thresholds as _exit_thresholds
from options_m.store import Store


class _Collector:
    """A notifier that records what the agent would have sent to Telegram."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, text: str) -> None:
        self.messages.append(text)


def _agent(
    store: Store, notifier: _Collector | None = None, **overrides: Any
) -> StrategistAgent:
    settings = Settings(database_url=None, **overrides)
    return StrategistAgent(settings, store, llm=None, notifier=notifier)  # type: ignore[arg-type]


def _store() -> Store:
    return Store(Database(Settings(database_url=None)))


def _detail() -> dict[str, Any]:
    return {"skipped": None}


async def _seed_candidates(store: Store, *symbols: str) -> None:
    await store.save_candidates(
        [
            {"symbol": symbol, "score": float(len(symbols) - index)}
            for index, symbol in enumerate(symbols)
        ]
    )


async def test_top_candidate_is_returned_when_nothing_blocks_it() -> None:
    store = _store()
    await _seed_candidates(store, "SPY", "QQQ", "IWM")

    candidate = await _agent(store)._pick_candidate(datetime.now(UTC), _detail())

    assert candidate is not None
    assert candidate["symbol"] == "SPY"


async def test_a_symbol_proposed_within_the_cooldown_is_skipped() -> None:
    store = _store()
    await _seed_candidates(store, "SPY", "QQQ", "IWM")
    # no_action so the active-proposal guard is not what does the skipping —
    # this isolates the cooldown.
    proposal_id = await store.save_proposal(underlying="SPY", intent={}, evidence={})
    await store.update_proposal_status(proposal_id, "no_action")

    candidate = await _agent(store)._pick_candidate(datetime.now(UTC), _detail())

    assert candidate is not None
    assert candidate["symbol"] == "QQQ"


async def test_a_symbol_at_its_per_day_cap_is_skipped_even_past_the_cooldown() -> None:
    store = _store()
    await _seed_candidates(store, "SPY", "QQQ", "IWM")
    now = datetime.now(UTC)
    for _ in range(3):  # max_proposals_per_symbol_per_day default
        # rejected is a trade attempt that does not hold the active slot.
        proposal_id = await store.save_proposal(underlying="SPY", intent={}, evidence={})
        await store.update_proposal_status(proposal_id, "rejected")
        store._memory_proposals[proposal_id]["ts"] = now - timedelta(hours=5)

    candidate = await _agent(store)._pick_candidate(now, _detail())

    assert candidate is not None
    assert candidate["symbol"] == "QQQ"


async def test_holds_on_one_symbol_do_not_hit_its_per_day_cap() -> None:
    store = _store()
    await _seed_candidates(store, "SPY", "QQQ", "IWM")
    now = datetime.now(UTC)
    for _ in range(6):
        proposal_id = await store.save_proposal(
            underlying="SPY", intent={"action": "hold"}, evidence={}, status="no_action"
        )
        store._memory_proposals[proposal_id]["ts"] = now - timedelta(hours=5)

    candidate = await _agent(store)._pick_candidate(now, _detail())

    assert candidate is not None
    assert candidate["symbol"] == "SPY"


async def test_the_global_per_day_cap_stops_all_work_and_records_the_reason() -> None:
    store = _store()
    await _seed_candidates(store, "SPY", "QQQ", "IWM")
    now = datetime.now(UTC)
    for index in range(5):
        proposal_id = await store.save_proposal(
            underlying=f"X{index}", intent={}, evidence={}
        )
        store._memory_proposals[proposal_id]["ts"] = now - timedelta(hours=3)

    detail: dict[str, Any] = {"skipped": None}
    candidate = await _agent(store, max_proposals_per_day=5)._pick_candidate(now, detail)

    assert candidate is None
    assert detail["skipped"] == "proposal_cap"


async def test_holds_and_llm_failures_do_not_consume_the_daily_cap() -> None:
    """A day of no_action / llm_failed used to silence the strategist.

    Production hit max_proposals_per_day=40 around 15:51 UTC after two hours
    of 180s ticks that mostly wrote holds. The rest of the session then
    skipped with proposal_cap — no more looks, not even at names the matrix
    still wanted. The cap is for trade attempts, not for 'I looked and held'.
    """
    store = _store()
    await _seed_candidates(store, "SPY", "QQQ", "IWM")
    now = datetime.now(UTC)
    for index in range(8):
        proposal_id = await store.save_proposal(
            underlying=f"H{index}", intent={"action": "hold"}, evidence={}, status="no_action"
        )
        store._memory_proposals[proposal_id]["ts"] = now - timedelta(hours=3)
    failed = await store.save_proposal(
        underlying="QQQ", intent={}, evidence={}, status="llm_failed"
    )
    store._memory_proposals[failed]["ts"] = now - timedelta(hours=3)

    detail: dict[str, Any] = {"skipped": None}
    candidate = await _agent(store, max_proposals_per_day=5)._pick_candidate(now, detail)

    assert candidate is not None
    assert detail["skipped"] is None


async def test_an_active_proposal_blocks_its_underlying_regardless_of_age() -> None:
    """A pending / dry-run-approved / submitted proposal older than the
    cooldown still holds the slot — H1's re-proposal window must stay closed."""
    store = _store()
    await _seed_candidates(store, "SPY", "QQQ", "IWM")
    now = datetime.now(UTC)
    proposal_id = await store.save_proposal(underlying="SPY", intent={}, evidence={})
    await store.update_proposal_status(proposal_id, "submitted")
    store._memory_proposals[proposal_id]["ts"] = now - timedelta(days=3)

    candidate = await _agent(store)._pick_candidate(now, _detail())

    assert candidate is not None
    assert candidate["symbol"] == "QQQ"


# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------


async def _seed_position(store: Store, symbol: str, payload: dict[str, Any]) -> None:
    await store.upsert_position(symbol, payload)


async def test_a_close_decision_is_announced() -> None:
    store = _store()
    collector = _Collector()
    # -60% against a 50% stop is unambiguously an exit.
    await _seed_position(store, "SPY", {"market_value": 400.0, "unrealized_pl": -600.0,
                                        "pnl_pct": -0.60, "strategy": "long_call"})
    agent = _agent(store, collector)
    detail = await agent._evaluate_close_proposals()

    assert detail.get("close_proposals") == 1
    assert len(collector.messages) == 1
    assert "SPY" in collector.messages[0]


async def test_close_decisions_are_silent_when_decisions_are_off() -> None:
    store = _store()
    collector = _Collector()
    await _seed_position(store, "SPY", {"market_value": 400.0, "unrealized_pl": -600.0,
                                        "pnl_pct": -0.60, "strategy": "long_call"})
    agent = _agent(store, collector, telegram_notify_decisions=False)
    await agent._evaluate_close_proposals()
    assert collector.messages == []


async def test_the_strategist_works_without_a_notifier() -> None:
    store = _store()
    await _seed_position(store, "SPY", {"market_value": 400.0, "unrealized_pl": -600.0,
                                        "pnl_pct": -0.60, "strategy": "long_call"})
    detail = await _agent(store)._evaluate_close_proposals()
    assert detail.get("close_proposals") == 1


# ---------------------------------------------------------------------------
# Exit rules — the ladder in _close_reason
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    return Settings(database_url=None, **overrides)


def test_no_condition_met_is_not_an_exit() -> None:
    payload = {"strategy": "long_call", "pnl_pct": 0.10, "min_dte": 30}
    assert _close_reason(payload, _settings()) is None


def test_a_position_in_its_last_days_is_closed_whatever_it_is() -> None:
    """Expiry is a hard floor for every family: an ITM option carried into
    expiration turns into a stock position nobody sized for."""
    for strategy in ("long_call", "call_debit_spread", "iron_condor"):
        payload = {"strategy": strategy, "pnl_pct": 0.0, "min_dte": 2}
        assert _close_reason(payload, _settings()) == "expiry_hard_stop"


def test_short_premium_is_closed_at_the_gamma_boundary() -> None:
    """Read off the setting, not off a literal.

    exit_dte_short_premium has to stay below dte_target_min or a credit
    structure is born inside its own stop, so the boundary moves whenever the
    entry window does — a hardcoded 21 here would fail for a config change that
    is entirely correct.
    """
    settings = _settings()
    payload = {
        "strategy": "put_credit_spread",
        "pnl_pct": 0.10,
        "min_dte": settings.exit_dte_short_premium,
    }
    assert _close_reason(payload, settings) == "dte_stop"


def test_the_short_premium_stop_stays_below_the_entry_window() -> None:
    """Otherwise every credit structure opens already tripping its own exit.

    StrategistAgent would propose a close on the first tick after the fill, and
    the book would pay two spreads to hold nothing.
    """
    settings = _settings()

    assert settings.exit_dte_short_premium < settings.dte_target_min


def test_the_gamma_boundary_does_not_apply_to_bought_premium() -> None:
    """A debit or long structure paid for its convexity — being inside the
    short-premium DTE window is not a reason to hand it back."""
    settings = _settings()
    for strategy in ("long_call", "call_debit_spread"):
        payload = {
            "strategy": strategy,
            "pnl_pct": 0.10,
            "min_dte": settings.exit_dte_short_premium,
        }
        assert _close_reason(payload, settings) is None


def test_expiry_outranks_a_profit_target() -> None:
    """Both rungs are tripped; the more urgent one is what gets reported."""
    payload = {"strategy": "iron_condor", "pnl_pct": 0.90, "min_dte": 1}
    assert _close_reason(payload, _settings()) == "expiry_hard_stop"


def test_a_missing_min_dte_leaves_the_other_rungs_working() -> None:
    payload = {"strategy": "long_call", "pnl_pct": 1.00}
    assert _close_reason(payload, _settings()) == "profit_target"


def test_each_family_is_measured_against_its_own_thresholds() -> None:
    settings = _settings()
    # A credit structure takes half the credit; the same +0.50 is not yet an
    # exit for a long option, whose target is a double.
    credit = {"strategy": "put_credit_spread", "pnl_pct": 0.50, "min_dte": 40}
    long_call = {"strategy": "long_call", "pnl_pct": 0.50, "min_dte": 40}
    assert _close_reason(credit, settings) == "profit_target"
    assert _close_reason(long_call, settings) is None

    # And the credit stop is looser than the long one: -0.60 stops a long
    # position but is still inside a credit structure's 1x-credit stop.
    assert _close_reason({**credit, "pnl_pct": -0.60}, settings) is None
    assert _close_reason({**long_call, "pnl_pct": -0.60}, settings) == "stop_loss"


def test_debit_verticals_are_recognised_under_either_spelling() -> None:
    """matrix.py emits call_debit_spread; the legacy name is still accepted by
    StrategyIntent, so both have to resolve to the same family."""
    settings = _settings()
    for strategy in ("call_debit_spread", "debit_call_spread"):
        assert _exit_thresholds(strategy, settings) == (
            settings.exit_debit_profit_target_pct,
            settings.exit_debit_stop_loss_pct,
        )


def test_an_unresolved_strategy_falls_back_to_the_symmetric_pair() -> None:
    """Enrichment can fail to link a position to its proposal. That position
    still exits — on the thresholds every position used before families."""
    settings = _settings()
    assert _exit_thresholds("", settings) == (
        settings.exit_profit_target_pct,
        settings.exit_stop_loss_pct,
    )
    assert _close_reason({"strategy": "", "pnl_pct": 0.50}, settings) == "profit_target"
    assert _close_reason({"pnl_pct": -0.50}, settings) == "stop_loss"


def test_the_calendar_stop_still_backstops_everything() -> None:
    opened = datetime.now(UTC) - timedelta(days=31)
    payload = {
        "strategy": "long_call",
        "pnl_pct": 0.0,
        "min_dte": 60,
        "opened_at": opened.isoformat(),
    }
    assert _close_reason(payload, _settings()) == "time_stop"


def test_the_thresholds_are_configurable() -> None:
    payload = {"strategy": "iron_condor", "pnl_pct": 0.30, "min_dte": 40}
    assert _close_reason(payload, _settings()) is None
    assert _close_reason(payload, _settings(exit_credit_profit_target_pct=0.25)) == (
        "profit_target"
    )
