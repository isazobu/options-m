"""StrategistAgent candidate selection.

The strategist runs every few minutes for a whole session. Under DRY_RUN a
name never becomes an open position, so without a cooldown and hard caps the
top-scored symbol is re-proposed — a fresh LLM call and a near-duplicate
proposal — on every single tick (review finding H2).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from options_m.agents.strategist import StrategistAgent
from options_m.config import Settings
from options_m.db import Database
from options_m.store import Store


def _agent(store: Store, **overrides: Any) -> StrategistAgent:
    settings = Settings(database_url=None, **overrides)
    return StrategistAgent(settings, store, llm=None)  # type: ignore[arg-type]


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
        proposal_id = await store.save_proposal(underlying="SPY", intent={}, evidence={})
        await store.update_proposal_status(proposal_id, "no_action")
        store._memory_proposals[proposal_id]["ts"] = now - timedelta(hours=5)

    candidate = await _agent(store)._pick_candidate(now, _detail())

    assert candidate is not None
    assert candidate["symbol"] == "QQQ"


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
