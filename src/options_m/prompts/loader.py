"""Prompt loader with a path-escape guard.

Prompts live in this directory as ``<name>.yaml`` files. One file per LLM
call site (or small group of related call sites). Each file has the shape::

    description: >
      One or two lines on what this prompt is for.
    params:                # optional — decoding params the call site should use
      temperature: 0.2
      max_tokens: 120
    templates:
      system: |
        ...
      user: |
        ... {variable} ...

:func:`load` returns a :class:`Prompt`. Individual templates are rendered with
:meth:`Prompt.render`, which uses :func:`str.format_map` so templates reference
variables by name. A template with no variables is returned verbatim; one that
contains literal ``{`` / ``}`` must double them (``{{`` / ``}}``).

A path-escape attempt is a serious bug, not a user error — raise rather than
sanitise. This mirrors AlpacaTradingAgent's ``tradingagents/prompts/loader.py``
pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

_SAFE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_PROMPTS_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Prompt:
    """A parsed prompt file: its templates plus optional call-site params."""

    name: str
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)

    def render(self, key: str = "user", /, **kwargs: object) -> str:
        """Render template ``key`` with ``kwargs`` substituted by name.

        Raises :exc:`KeyError` if the file has no template called ``key``.
        A template with no placeholders is returned unchanged (no ``kwargs``
        needed); ``str.format_map`` is only applied when ``kwargs`` are given.
        """
        try:
            template = self.templates[key]
        except KeyError:
            msg = f"prompt {self.name!r} has no template {key!r}"
            raise KeyError(msg) from None
        return template.format_map(kwargs) if kwargs else template


@cache
def load(name: str) -> Prompt:
    """Load and parse prompt file ``name`` (without the ``.yaml`` extension).

    ``name`` must be a bare filename — no path separators, dots, or slashes.
    Raises :exc:`ValueError` if the name contains anything that could escape
    the prompts directory, and :exc:`ValueError` if the file is malformed.
    """
    if not _SAFE_NAME_RE.match(name):
        msg = f"prompt name must be [a-z0-9_-] only, got {name!r}"
        raise ValueError(msg)

    path = _PROMPTS_DIR / f"{name}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"prompt file {path.name} must be a YAML mapping, got {type(raw).__name__}"
        raise ValueError(msg)

    templates = raw.get("templates") or {}
    if not isinstance(templates, dict) or not templates:
        msg = f"prompt file {path.name} must define a non-empty 'templates' mapping"
        raise ValueError(msg)
    if not all(isinstance(v, str) for v in templates.values()):
        msg = f"prompt file {path.name}: every template must be a string"
        raise ValueError(msg)

    params = raw.get("params") or {}
    if not isinstance(params, dict):
        msg = f"prompt file {path.name}: 'params' must be a mapping"
        raise ValueError(msg)

    return Prompt(
        name=name,
        description=str(raw.get("description") or "").strip(),
        params=params,
        templates={k: _strip_trailing_newline(v) for k, v in templates.items()},
    )


def _strip_trailing_newline(text: str) -> str:
    """YAML block scalars (``|``) keep one trailing newline; drop it so a
    rendered prompt does not end with stray whitespace."""
    return text[:-1] if text.endswith("\n") else text
