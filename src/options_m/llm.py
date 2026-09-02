"""Featherless LLM client.

Supports two call patterns:

- :meth:`FeatherlessLlm.chat_completion` — free-form chat with optional tool
  definitions. Used by the dashboard chat.
- :meth:`FeatherlessLlm.complete_json` — structured output with Pydantic
  validation and one repair retry. Thinking is turned off so a reasoning
  model cannot spend the token budget on CoT and return empty content.
  Used by StrategistAgent. Raises :exc:`LlmContractError` on two consecutive
  failures; never silently falls back to free text for a trade decision.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from options_m.prompts import loader as prompt_loader

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_CONTRACT_PROMPT = prompt_loader.load("llm_contract")

# Structured output is a three-field JSON object. Thinking models (DeepSeek V4
# Flash in particular) spend the whole max_tokens budget on CoT and return
# empty content — production recorded that as llm_failed after ~44s.
_STRUCTURED_OUTPUT_EXTRAS: dict[str, Any] = {
    "chat_template_kwargs": {
        "enable_thinking": False,
        "thinking": False,
    },
}
_THINK_TAG = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

T = TypeVar("T", bound=BaseModel)


class LlmError(RuntimeError):
    """Raised when Featherless cannot be reached or returns something unusable."""


class LlmContractError(LlmError):
    """Raised when the LLM fails to produce valid structured output after one repair retry.

    A failed decision means *no trade*, recorded as proposals.status='llm_failed'.
    Never fall back to free text for a trade decision.
    """


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LlmResult:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning_content: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class FeatherlessLlm:
    """Featherless OpenAI-compatible client with chat and structured-output support."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        timeout_seconds: float,
        daily_token_budget: int = 100_000,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._daily_token_budget = daily_token_budget
        self._budget_date: date | None = None
        self._tokens_used_today: int = 0
        self.last_prompt_tokens: int = 0
        self.last_completion_tokens: int = 0
        self.last_error: str | None = None

    @property
    def is_enabled(self) -> bool:
        """Whether both credentials and a model id are configured."""
        return bool(self._api_key and self._model)

    @property
    def daily_budget_exhausted(self) -> bool:
        """True if today's token budget has been fully consumed."""
        today = datetime.now(UTC).date()
        if self._budget_date != today:
            self._budget_date = today
            self._tokens_used_today = 0
        return self._tokens_used_today >= self._daily_token_budget

    def _charge_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        today = datetime.now(UTC).date()
        if self._budget_date != today:
            self._budget_date = today
            self._tokens_used_today = 0
        self._tokens_used_today += prompt_tokens + completion_tokens

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 800,
        temperature: float = 0.2,
        extra_body: dict[str, Any] | None = None,
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
        if extra_body:
            payload.update(extra_body)

        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions", json=payload, headers=headers
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            msg = f"featherless request failed: {exc}"
            self.last_error = msg
            raise LlmError(msg) from exc

        try:
            body = response.json()
            choice = body["choices"][0]["message"]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            msg = "featherless returned an unreadable response"
            self.last_error = msg
            raise LlmError(msg) from exc

        usage = body.get("usage") if isinstance(body, dict) else None
        prompt_tokens = 0
        completion_tokens = 0
        if isinstance(usage, dict):
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            self._charge_tokens(prompt_tokens, completion_tokens)
        self.last_prompt_tokens = prompt_tokens
        self.last_completion_tokens = completion_tokens
        self.last_error = None

        return LlmResult(
            content=_coerce_text(choice.get("content")),
            tool_calls=_parse_tool_calls(choice),
            reasoning_content=_coerce_text(choice.get("reasoning_content")) or None,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


    async def complete_json(
        self,
        *,
        schema: type[T],
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> T:
        """Call Featherless and return a validated Pydantic instance.

        One repair retry on validation failure. Raises :exc:`LlmContractError`
        after two consecutive failures — the caller must record this and skip
        the trade, never fall back to free text.
        """
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        full_user = _CONTRACT_PROMPT.render("schema_suffix", user=user, schema_json=schema_json)

        last_error: Exception | None = None
        raw_text: str | None = None
        for attempt in range(2):
            if attempt == 1 and raw_text is not None:
                # Repair attempt: show the model its own bad output + the error.
                repair_user = _CONTRACT_PROMPT.render(
                    "repair", error=last_error, raw_text=raw_text
                )
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": full_user},
                    {"role": "assistant", "content": raw_text},
                    {"role": "user", "content": repair_user},
                ]
            else:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": full_user},
                ]

            t0 = time.monotonic()
            try:
                result = await self.chat_completion(
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    extra_body=_STRUCTURED_OUTPUT_EXTRAS,
                )
            except LlmError as exc:
                last_error = exc
                continue

            raw_text = _visible_text(result)
            json_str = _extract_json(raw_text)
            if json_str is None:
                last_error = ValueError("no JSON object found in response")
                continue

            try:
                data = json.loads(json_str)
                instance = schema.model_validate(data)
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.debug(
                    "llm.complete_json succeeded",
                    extra={"schema": schema.__name__, "attempt": attempt, "latency_ms": latency_ms},
                )
                self.last_error = None
                return instance
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc

        message = f"LLM failed to produce valid {schema.__name__} after 2 attempts: {last_error}"
        self.last_error = message
        raise LlmContractError(message) from last_error


def _coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _strip_think_tags(text: str) -> str:
    return _THINK_TAG.sub("", text).strip()


def _visible_text(result: LlmResult) -> str:
    text = _strip_think_tags(result.content or "")
    if text:
        return text
    return _strip_think_tags(result.reasoning_content or "")


def _extract_json(text: str) -> str | None:
    """Extract the first valid JSON object from prose or code-fenced text."""
    # Try ```json ... ``` or ``` ... ``` fences first.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    # Fall back to finding the first { ... } pair by counting brace depth.
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


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
