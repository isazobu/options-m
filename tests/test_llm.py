"""Tests for the minimal Featherless client.

Two surfaces: ``chat_completion``, which the dashboard chat drives, and
``complete_json``, which is the only path a trade proposal can come down. The
second went untested for a long time even though it is the one that decides
whether a malformed model reply becomes a skipped tick or an order — so the
repair retry, its "exactly once" bound, and the ``LlmContractError`` that ends it
are all pinned here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, Field

from options_m.llm import FeatherlessLlm, LlmContractError, LlmError, _extract_json
from options_m.prompts import loader as prompt_loader

# Captured before any test monkeypatches httpx.AsyncClient, so the patched
# factory below can still construct a real client around a mock transport.
_RealAsyncClient = httpx.AsyncClient


class _Answer(BaseModel):
    """Stand-in for RegimeRead: one free field, one bounded one."""

    thesis: str
    conviction: float = Field(ge=0.0, le=1.0)


def _scripted(
    *contents: str,
) -> tuple[Callable[[httpx.Request], httpx.Response], list[dict[str, Any]]]:
    """Reply with each content in turn, recording every outbound request body.

    The last content repeats if the client asks more times than were scripted,
    so a test that expects two attempts fails on the count rather than on a
    StopIteration from the transport.
    """
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        content = contents[min(len(sent) - 1, len(contents) - 1)]
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler, sent


def _client() -> FeatherlessLlm:
    return FeatherlessLlm(
        api_key="key", base_url="https://featherless.test/v1", model="m", timeout_seconds=5.0
    )


def test_is_enabled_requires_both_key_and_model() -> None:
    assert FeatherlessLlm(
        api_key="k", base_url="https://x", model="m", timeout_seconds=1.0
    ).is_enabled
    assert not FeatherlessLlm(
        api_key=None, base_url="https://x", model="m", timeout_seconds=1.0
    ).is_enabled
    assert not FeatherlessLlm(
        api_key="k", base_url="https://x", model="", timeout_seconds=1.0
    ).is_enabled


async def test_chat_completion_raises_when_unconfigured() -> None:
    llm = FeatherlessLlm(api_key=None, base_url="https://x", model="", timeout_seconds=1.0)
    with pytest.raises(LlmError):
        await llm.chat_completion([])


async def test_chat_completion_parses_plain_content(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    llm = FeatherlessLlm(
        api_key="key", base_url="https://featherless.test/v1", model="m", timeout_seconds=5.0
    )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
    )

    result = await llm.chat_completion([{"role": "user", "content": "hi"}])

    assert result.content == "hello"
    assert result.tool_calls == []


async def test_chat_completion_parses_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "get_account_summary",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    llm = FeatherlessLlm(
        api_key="key", base_url="https://featherless.test/v1", model="m", timeout_seconds=5.0
    )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
    )

    result = await llm.chat_completion([{"role": "user", "content": "hi"}], tools=[{"a": 1}])

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "get_account_summary"
    assert result.tool_calls[0].id == "call_1"


async def test_a_transport_failure_raises_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    llm = FeatherlessLlm(
        api_key="key", base_url="https://featherless.test/v1", model="m", timeout_seconds=5.0
    )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(LlmError):
        await llm.chat_completion([{"role": "user", "content": "hi"}])


async def test_an_unreadable_response_raises_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    llm = FeatherlessLlm(
        api_key="key", base_url="https://featherless.test/v1", model="m", timeout_seconds=5.0
    )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(LlmError):
        await llm.chat_completion([{"role": "user", "content": "hi"}])


# ---- complete_json --------------------------------------------------------


async def test_complete_json_returns_a_validated_model(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, sent = _scripted('{"thesis": "trend intact", "conviction": 0.6}')
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
    )

    result = await _client().complete_json(
        schema=_Answer, system="sys", user="usr", max_tokens=200, temperature=0.2
    )

    assert result.conviction == 0.6
    assert len(sent) == 1


@pytest.mark.parametrize(
    "reply",
    [
        '{"thesis": "t", "conviction": 0.6}',                              # bare
        'Sure!\n{"thesis": "t", "conviction": 0.6}\nHope that helps.',     # prose-wrapped
        '```json\n{"thesis": "t", "conviction": 0.6}\n```',                # fenced
        '```\n{"thesis": "t", "conviction": 0.6}\n```',                    # fenced, unlabelled
    ],
)
async def test_json_is_recovered_from_however_the_model_wraps_it(
    reply: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Models narrate. The contract is the JSON, not the packaging."""
    handler, sent = _scripted(reply)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
    )

    result = await _client().complete_json(
        schema=_Answer, system="sys", user="usr", max_tokens=200, temperature=0.2
    )

    assert result.thesis == "t"
    assert len(sent) == 1


async def test_a_validation_failure_triggers_exactly_one_repair_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair turn must show the model its own bad output and the error —
    otherwise the retry is just a second roll of the same dice."""
    handler, sent = _scripted("I think it's bullish.", '{"thesis": "t", "conviction": 0.6}')
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
    )

    result = await _client().complete_json(
        schema=_Answer, system="sys", user="usr", max_tokens=200, temperature=0.2
    )

    assert result.conviction == 0.6
    assert len(sent) == 2
    repair = sent[1]["messages"]
    assert [m["role"] for m in repair] == ["system", "user", "assistant", "user"]
    assert repair[2]["content"] == "I think it's bullish."
    assert "could not be parsed" in repair[3]["content"]
    assert "I think it's bullish." in repair[3]["content"]


async def test_two_unusable_replies_raise_rather_than_trying_a_third_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed decision means no trade, never a free-text fallback."""
    handler, sent = _scripted("no json here", "still no json")
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(LlmContractError):
        await _client().complete_json(
            schema=_Answer, system="sys", user="usr", max_tokens=200, temperature=0.2
        )

    assert len(sent) == 2


async def test_schema_validation_not_just_json_parsing_gates_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conviction of 4.2 is valid JSON and a nonsense trade signal."""
    handler, _sent = _scripted('{"thesis": "t", "conviction": 4.2}')
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(LlmContractError, match="conviction"):
        await _client().complete_json(
            schema=_Answer, system="sys", user="usr", max_tokens=200, temperature=0.2
        )


async def test_the_rendered_prompt_file_is_a_prefix_of_what_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``complete_json`` appends the JSON schema to the caller's user message.
    The prompt file owns the front of that string and the client owns the back;
    neither may reformat the other's half."""
    handler, sent = _scripted('{"thesis": "t", "conviction": 0.6}')
    user = prompt_loader.load(
        "strategist", symbol="SPY", evidence_json="{}", conviction_floor="0.55"
    ).user
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
    )

    await _client().complete_json(
        schema=_Answer, system="sys", user=user, max_tokens=200, temperature=0.2
    )

    sent_user = sent[0]["messages"][1]["content"]
    assert sent_user.startswith(user)
    assert "Output only valid JSON matching this schema" in sent_user[len(user) :]
    assert sent[0]["messages"][0] == {"role": "system", "content": "sys"}


async def test_complete_json_raises_a_contract_error_when_unconfigured() -> None:
    llm = FeatherlessLlm(
        api_key=None, base_url="https://x", model="m", timeout_seconds=1.0
    )

    with pytest.raises(LlmContractError):
        await llm.complete_json(
            schema=_Answer, system="s", user="u", max_tokens=10, temperature=0.2
        )


@pytest.mark.parametrize(
    "text",
    [
        "no braces at all",
        "{unbalanced",
        "",
    ],
)
def test_extract_json_returns_none_when_there_is_no_object(text: str) -> None:
    assert _extract_json(text) is None
