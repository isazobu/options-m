"""Tests for the minimal Featherless client used by the dashboard chat."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import BaseModel

from options_m.llm import FeatherlessLlm, LlmContractError, LlmError


class _Regime(BaseModel):
    thesis: str
    invalidation: str
    conviction: float


_VALID_JSON = json.dumps({"thesis": "range-bound", "invalidation": "break", "conviction": 0.7})

# Captured before any test monkeypatches httpx.AsyncClient, so the patched
# factory below can still construct a real client around a mock transport.
_RealAsyncClient = httpx.AsyncClient


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


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler))
    )


def _deepseek() -> FeatherlessLlm:
    return FeatherlessLlm(
        api_key="key",
        base_url="https://featherless.test/v1",
        model="deepseek-ai/DeepSeek-V4-Flash-0731",
        timeout_seconds=5.0,
    )


async def test_complete_json_turns_thinking_off_for_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DeepSeek V4 thinks by default. Thinking burns max_tokens=1024 and
    leaves content empty, which production recorded as llm_failed after 44s."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": _VALID_JSON}}]})

    _patch_client(monkeypatch, handler)
    parsed = await _deepseek().complete_json(
        schema=_Regime, system="s", user="u", max_tokens=1024, temperature=0.2
    )

    kwargs = captured.get("chat_template_kwargs")
    assert isinstance(kwargs, dict)
    assert kwargs.get("enable_thinking") is False
    assert parsed.conviction == 0.7


async def test_complete_json_reads_json_from_reasoning_content_when_content_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": _VALID_JSON,
                        }
                    }
                ],
                "usage": {"prompt_tokens": 80, "completion_tokens": 900},
            },
        )

    _patch_client(monkeypatch, handler)
    llm = _deepseek()
    parsed = await llm.complete_json(
        schema=_Regime, system="s", user="u", max_tokens=1024, temperature=0.2
    )

    assert parsed.thesis == "range-bound"
    assert llm.last_prompt_tokens == 80
    assert llm.last_completion_tokens == 900


async def test_complete_json_strips_think_tags_before_extracting_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": f"<think>I will emit {{not json}}</think>\n{_VALID_JSON}",
                        }
                    }
                ]
            },
        )

    _patch_client(monkeypatch, handler)
    parsed = await _deepseek().complete_json(
        schema=_Regime, system="s", user="u", max_tokens=1024, temperature=0.2
    )

    assert parsed.invalidation == "break"


async def test_complete_json_records_the_error_when_both_attempts_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    _patch_client(monkeypatch, handler)
    llm = _deepseek()
    with pytest.raises(LlmContractError):
        await llm.complete_json(
            schema=_Regime, system="s", user="u", max_tokens=1024, temperature=0.2
        )

    assert llm.last_error is not None
    assert "no JSON" in llm.last_error
