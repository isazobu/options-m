"""Minimal Featherless client, scoped to the dashboard chat feature.

Only what chat needs: one OpenAI-compatible ``chat/completions`` call with
optional tool (function-calling) definitions. Phase 3's LLM crew needs more —
token-budget tracking, an ``llm_calls`` table, a JSON-repair retry loop for
structured trade proposals — and should extend this module rather than
replace it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class LlmError(RuntimeError):
    """Raised when Featherless cannot be reached or returns something unusable."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LlmResult:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class FeatherlessLlm:
    """Thin wrapper over Featherless's OpenAI-compatible chat/completions endpoint."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds

    @property
    def is_enabled(self) -> bool:
        """Whether both credentials and a model id are configured."""
        return bool(self._api_key and self._model)

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 800,
        temperature: float = 0.2,
    ) -> LlmResult:
        if not self.is_enabled:
            msg = "Featherless is not configured (missing api key or model)"
            raise LlmError(msg)

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions", json=payload, headers=headers
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            msg = f"featherless request failed: {exc}"
            raise LlmError(msg) from exc

        try:
            body = response.json()
            choice = body["choices"][0]["message"]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            msg = "featherless returned an unreadable response"
            raise LlmError(msg) from exc

        return LlmResult(content=choice.get("content"), tool_calls=_parse_tool_calls(choice))


def _parse_tool_calls(choice: dict[str, Any]) -> list[ToolCall]:
    raw_tool_calls = choice.get("tool_calls") or []
    if not isinstance(raw_tool_calls, list):
        return []

    tool_calls: list[ToolCall] = []
    for index, raw in enumerate(raw_tool_calls):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str):
            continue
        arguments: dict[str, Any] = {}
        arguments_raw = function.get("arguments")
        if isinstance(arguments_raw, str) and arguments_raw:
            try:
                decoded = json.loads(arguments_raw)
            except json.JSONDecodeError:
                logger.warning("chat tool call had unparseable arguments", extra={"name": name})
                decoded = None
            if isinstance(decoded, dict):
                arguments = decoded
        call_id = raw.get("id")
        tool_calls.append(
            ToolCall(
                id=call_id if isinstance(call_id, str) else f"call_{index}",
                name=name,
                arguments=arguments,
            )
        )
    return tool_calls
