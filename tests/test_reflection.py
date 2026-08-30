"""Tests for ReflectionAgent's two post-mortem prompts.

Pass B used to decide what to call a proposal with an inline conditional inside
the system message itself — ``'held (no trade taken)' if status == 'no_action'
else 'rejected'``. Anything that was not ``no_action`` was therefore described to
the model as *rejected*, an assumption nothing stated and nothing checked. The
phrasing now comes from an explicit table and reaches the prompt as one variable,
so these tests pin both branches and the fallback the old code could not express.
"""

from __future__ import annotations

from typing import Any

import pytest

from options_m.agents.reflection import ReflectionAgent
from options_m.config import Settings
from options_m.db import Database
from options_m.llm import LlmError, LlmResult
from options_m.store import Store


class _RecordingLlm:
    """Captures the exact messages and parameters that reached the client."""

    def __init__(self, content: str = "Lesson: size down after a gap.") -> None:
        self.is_enabled = True
        self.calls: list[dict[str, Any]] = []
        self._content = content

    async def chat_completion(
        self, messages: list[dict[str, Any]], **kwargs: Any
    ) -> LlmResult:
        self.calls.append({"messages": [dict(m) for m in messages], **kwargs})
        return LlmResult(content=self._content, tool_calls=[])


class _FailingLlm:
    is_enabled = True

    def __init__(self) -> None:
        self.calls = 0

    async def chat_completion(self, *args: Any, **kwargs: Any) -> LlmResult:
        self.calls += 1
        raise LlmError("down")


def _agent(llm: Any) -> tuple[ReflectionAgent, Store]:
    settings = Settings(database_url=None)
    store = Store(Database(settings))
    return ReflectionAgent(settings, store, llm), store


def _system_of(llm: _RecordingLlm, index: int = 0) -> str:
    return str(llm.calls[index]["messages"][0]["content"])


def _user_of(llm: _RecordingLlm, index: int = 0) -> str:
    return str(llm.calls[index]["messages"][1]["content"])


async def _with_proposal(store: Store, status: str, *, error: str | None = None) -> int:
    proposal_id = await store.save_proposal(
        underlying="SPY",
        intent={},
        evidence={"symbol": "SPY"},
        status=status,
        llm_read={"thesis": "chop with rich IV", "conviction": 0.31},
    )
    if error is not None:
        await store.update_proposal_status(proposal_id, status, error=error)
    return proposal_id


# ---------------------------------------------------------------------------
# Pass B — how a proposal is described to the model


async def test_a_held_proposal_is_described_as_held_not_rejected() -> None:
    llm = _RecordingLlm()
    agent, store = _agent(llm)
    await _with_proposal(store, "no_action")

    await agent.step()

    system = _system_of(llm)
    assert "held (no trade taken)" in system
    assert "rejected" not in system


async def test_a_rejected_proposal_is_described_as_rejected() -> None:
    llm = _RecordingLlm()
    agent, store = _agent(llm)
    await _with_proposal(store, "rejected", error="spread too wide")

    await agent.step()

    system = _system_of(llm)
    assert "rejected" in system
    assert "held" not in system
    assert "spread too wide" in _user_of(llm)


async def test_the_two_branches_differ_only_in_the_status_phrase() -> None:
    """One template with one variable, not two prompts free to drift apart."""
    held_llm = _RecordingLlm()
    held_agent, held_store = _agent(held_llm)
    await _with_proposal(held_store, "no_action")
    await held_agent.step()

    rejected_llm = _RecordingLlm()
    rejected_agent, rejected_store = _agent(rejected_llm)
    await _with_proposal(rejected_store, "rejected")
    await rejected_agent.step()

    held = _system_of(held_llm).replace("held (no trade taken)", "<PHRASE>")
    rejected = _system_of(rejected_llm).replace("rejected", "<PHRASE>")
    assert held == rejected


async def test_an_unexpected_status_is_not_reported_to_the_model_as_rejected() -> None:
    """The old inline if/else called every non-``no_action`` status "rejected".
    Pass B only queries two statuses today, but the mapping no longer assumes it.
    """
    from options_m.agents.reflection import _STATUS_PHRASE_FALLBACK, _STATUS_PHRASES

    assert _STATUS_PHRASES.get("llm_failed") is None
    assert _STATUS_PHRASE_FALLBACK == "not acted on"
    assert "reject" not in _STATUS_PHRASE_FALLBACK


async def test_the_proposal_prompt_carries_the_thesis_and_conviction() -> None:
    llm = _RecordingLlm()
    agent, store = _agent(llm)
    await _with_proposal(store, "no_action")

    await agent.step()

    user = _user_of(llm)
    assert "SPY" in user
    assert "chop with rich IV" in user
    assert "0.31" in user


async def test_a_missing_rejection_reason_renders_as_not_applicable() -> None:
    llm = _RecordingLlm()
    agent, store = _agent(llm)
    await _with_proposal(store, "no_action")

    await agent.step()

    assert "Rejection reason: N/A" in _user_of(llm)


# ---------------------------------------------------------------------------
# Pass A — closed trades


async def test_a_filled_order_uses_the_trade_prompt_and_names_its_legs() -> None:
    llm = _RecordingLlm()
    agent, store = _agent(llm)
    proposal_id = await _with_proposal(store, "approved")
    await store.record_order(
        proposal_id=proposal_id,
        client_order_id="om-1",
        status="filled",
        request={"legs": [{"symbol": "SPY241220C00500000", "side": "buy"}]},
    )

    await agent.step()

    assert "filled options trade" in _system_of(llm)
    assert "SPY241220C00500000" in _user_of(llm)


async def test_an_order_with_no_legs_says_so_rather_than_saying_none() -> None:
    """The old f-string rendered the literal text ``legs=None``, which reads as a
    value rather than as an absence."""
    llm = _RecordingLlm()
    agent, store = _agent(llm)
    proposal_id = await _with_proposal(store, "approved")
    await store.record_order(
        proposal_id=proposal_id, client_order_id="om-2", status="filled", request={}
    )

    await agent.step()

    user = _user_of(llm)
    assert "legs=NO_DATA_AVAILABLE" in user
    assert "legs=None" not in user


# ---------------------------------------------------------------------------
# Call parameters and failure handling


@pytest.mark.parametrize("status", ["no_action", "rejected"])
async def test_the_lesson_prompts_stay_short_and_slightly_warm(status: str) -> None:
    """A post-mortem lesson is one or two sentences; the budget is part of the
    prompt file now, not a number loose in the agent."""
    llm = _RecordingLlm()
    agent, store = _agent(llm)
    await _with_proposal(store, status)

    await agent.step()

    assert llm.calls[0]["max_tokens"] == 120
    assert llm.calls[0]["temperature"] == 0.3


async def test_an_llm_failure_is_logged_and_skipped_rather_than_raised() -> None:
    """Reflection must never stop PositionManagerAgent or ExecutionAgent."""
    llm = _FailingLlm()
    agent, store = _agent(llm)
    await _with_proposal(store, "no_action")

    await agent.step()

    assert llm.calls >= 1
    assert await store.recent_lessons("SPY", 3) == []
