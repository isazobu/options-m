"""Tests for the YAML prompt loader (options_m.prompts.loader)."""

from __future__ import annotations

import pytest

from options_m.prompts import loader

_SHIPPED = ["strategist", "chat", "reflection", "llm_contract"]


@pytest.mark.parametrize("name", _SHIPPED)
def test_every_shipped_prompt_loads_with_templates(name: str) -> None:
    prompt = loader.load(name)
    assert prompt.name == name
    assert prompt.templates, "a prompt file must define at least one template"
    assert all(isinstance(v, str) for v in prompt.templates.values())


def test_render_substitutes_named_variables() -> None:
    rendered = loader.load("strategist").render(
        "user", symbol="AAPL", evidence_json='{"spot": 1}'
    )
    assert "Evidence pack for AAPL" in rendered
    # Literal braces in the JSON example survive as single braces, and the
    # substituted evidence blob is not re-interpreted as a format field.
    assert '"thesis": "..."' in rendered
    assert '{"spot": 1}' in rendered


def test_render_without_kwargs_returns_template_verbatim() -> None:
    prompt = loader.load("strategist")
    # No kwargs -> the template comes back exactly as stored, no format_map pass.
    assert prompt.render("system") == prompt.templates["system"]
    assert prompt.render("system").startswith("You are a quantitative options strategist")


def test_params_are_exposed() -> None:
    assert loader.load("strategist").params["temperature"] == 0.2
    assert loader.load("reflection").params["max_tokens"] == 120


def test_unknown_template_key_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        loader.load("chat").render("does_not_exist")


@pytest.mark.parametrize("bad", ["../secrets", "a/b", "name.yaml", "Name", ""])
def test_path_escape_attempts_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="prompt name"):
        loader.load(bad)


def test_load_is_cached() -> None:
    assert loader.load("chat") is loader.load("chat")
