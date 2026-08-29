"""Prompt template loader with path-escape guard.

Templates live in this directory as ``<name>.md`` files. They are rendered
with :func:`str.format_map` so they can reference variables by name without
positional-index fragility.

A path-escape attempt is a serious bug, not a user error — raise rather than
sanitise. This mirrors AlpacaTradingAgent's ``tradingagents/prompts/loader.py``
pattern.
"""

from __future__ import annotations

import re
from pathlib import Path

_SAFE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_PROMPTS_DIR = Path(__file__).parent


def load(name: str, **kwargs: object) -> str:
    """Load and render prompt template ``name``.

    ``name`` must be a bare filename without extension — no path separators,
    dots, or slashes. Raises :exc:`ValueError` if the name contains anything
    that could escape the prompts directory.
    """
    if not _SAFE_NAME_RE.match(name):
        msg = f"prompt name must be [a-z0-9_-] only, got {name!r}"
        raise ValueError(msg)
    path = _PROMPTS_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    return text.format_map(kwargs) if kwargs else text
