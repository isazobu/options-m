"""Tests for the minimal Featherless client used by the dashboard chat."""

from __future__ import annotations

import httpx
import pytest

from options_m.llm import FeatherlessLlm, LlmError

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
