"""Prompt template loader: TOML frontmatter, ``string.Template`` bodies.

Every prompt this system sends a model lives in this directory as a ``<name>.md``
file, so a prompt is configuration rather than code — reviewable in a diff, and
editable without touching Python. (The "edit it without a redeploy" claim in
``docs/plan/phase-3-strategist-agent.md`` holds for a local, editable or
bind-mounted checkout only: the Docker image installs into a root-owned venv and
runs as ``app``, so the shipped copies are read-only there.)

Three deliberate choices, each of which was a bug first:

* **``string.Template`` (``$var``), not ``str.format_map``.** These prompts are
  full of JSON — the schema the model must copy, the evidence pack it reasons
  over — and ``format_map`` requires every literal brace to be doubled. One
  missed pair silently ships ``{{`` to the model or raises ``KeyError`` on a
  brace that was never a variable.
* **``substitute``, not ``safe_substitute``.** An unresolved ``$evidence_json``
  left in place reads to the model as a plausible empty pack, and it will answer
  anyway — a confident thesis grounded in nothing, which the matrix turns into a
  real proposal. There is no safe degraded mode for a trade-deciding prompt, and
  the system already has a correct "no decision this tick" path.
* **Exact variable-set equality.** A caller passing an extra keyword means either
  the prompt was edited and the caller was not, or the reverse. Both are the
  drift this module exists to prevent, so neither is tolerated.

A path-escape attempt is a serious bug, not a user error — raise rather than
sanitise.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Final

_SAFE_NAME_RE: Final = re.compile(r"^[a-z0-9_-]+$")
_PROMPTS_DIR: Final = Path(__file__).parent
_FRONTMATTER_DELIM: Final = "+++"
_SECTION_RE: Final = re.compile(r"^===[ \t]*(system|user)[ \t]*===[ \t]*$", re.MULTILINE)
_KNOWN_KEYS: Final = frozenset({"temperature", "max_tokens", "variables", "includes"})


class PromptError(ValueError):
    """A prompt file is malformed, missing, or rendered with the wrong variables.

    Subclasses :exc:`ValueError` so the path-escape contract this loader has
    always had — an unsafe name raises ``ValueError`` — still holds.
    """


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """One rendered LLM call: its messages and the parameters that go with them."""

    name: str
    system: str | None
    user: str
    max_tokens: int | None
    temperature: float | None

    def require_system(self) -> str:
        """The system message, or raise if this prompt declares none.

        Callers need the narrowing, and ruff bans a bare ``assert`` in ``src``.
        """
        if self.system is None:
            msg = f"prompt {self.name!r} has no === system === section"
            raise PromptError(msg)
        return self.system

    def require_max_tokens(self) -> int:
        if self.max_tokens is None:
            msg = f"prompt {self.name!r} declares no max_tokens"
            raise PromptError(msg)
        return self.max_tokens

    def require_temperature(self) -> float:
        if self.temperature is None:
            msg = f"prompt {self.name!r} declares no temperature"
            raise PromptError(msg)
        return self.temperature


@dataclass(frozen=True, slots=True)
class PromptSpec:
    """A parsed prompt file, cached until its mtime changes."""

    system: Template | None
    user: Template
    max_tokens: int | None
    temperature: float | None
    variables: tuple[str, ...]
    includes: tuple[str, ...]


_CACHE: dict[str, tuple[int, PromptSpec]] = {}
_FRAGMENT_CACHE: dict[str, tuple[int, str]] = {}


# ---------------------------------------------------------------------------
# Public API


def names() -> tuple[str, ...]:
    """Every ``.md`` file in this directory, prompts and fragments alike.

    Discovered rather than listed, so a prompt added later inherits the
    repo-wide invariant tests instead of quietly escaping them.
    """
    return tuple(sorted(path.stem for path in _PROMPTS_DIR.glob("*.md")))


def read(name: str, /) -> PromptSpec:
    """Parse ``name`` without rendering it. For tests and introspection."""
    return _compiled(name)


def load(name: str, /, **variables: object) -> RenderedPrompt:
    """Load and render prompt ``name``.

    ``name`` must be a bare filename without extension — no path separators,
    dots or slashes. Raises :exc:`PromptError` if the name could escape this
    directory, if the file is malformed, or if ``variables`` is not exactly the
    set the file declares.
    """
    compiled = _compiled(name)

    declared = set(compiled.variables)
    supplied = set(variables)
    if declared != supplied:
        msg = (
            f"prompt {name!r} variable mismatch: "
            f"missing={sorted(declared - supplied)} unexpected={sorted(supplied - declared)}"
        )
        raise PromptError(msg)

    mapping: dict[str, object] = dict(variables)
    for include in compiled.includes:
        mapping[include] = fragment(include)

    return RenderedPrompt(
        name=name,
        system=_render(compiled.system, mapping, name) if compiled.system is not None else None,
        user=_render(compiled.user, mapping, name),
        max_tokens=compiled.max_tokens,
        temperature=compiled.temperature,
    )


def fragment(name: str, /) -> str:
    """Return the raw text of a shared prompt fragment.

    A fragment carries no frontmatter and no variables; it is a block of text
    two prompts must state identically. The untrusted-text fence is one: the
    chat path prepends it to a tool result, the strategist prompt places it
    ahead of the evidence pack, and they must be the same bytes.
    """
    path = _path(name)
    mtime = _mtime(path, name)
    cached = _FRAGMENT_CACHE.get(name)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    text = _read_text(path)
    if text.lstrip().startswith(_FRONTMATTER_DELIM):
        msg = f"{name!r} has frontmatter — it is a prompt, load it with load()"
        raise PromptError(msg)
    template = Template(text)
    if not template.is_valid() or template.get_identifiers():
        msg = f"fragment {name!r} must not contain placeholders (write a literal $ as $$)"
        raise PromptError(msg)

    # Stripped for the same reason sections are: callers compose their own
    # separators, so a fragment's meaning must not depend on file layout.
    text = template.substitute({}).strip()
    _FRAGMENT_CACHE[name] = (mtime, text)
    return text


# ---------------------------------------------------------------------------
# Internals


def _path(name: str) -> Path:
    if not _SAFE_NAME_RE.match(name):
        msg = f"prompt name must be [a-z0-9_-] only, got {name!r}"
        raise PromptError(msg)
    return _PROMPTS_DIR / f"{name}.md"


def _mtime(path: Path, name: str) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError as exc:
        msg = f"no prompt file for {name!r} at {path.name}"
        raise PromptError(msg) from exc


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _compiled(name: str) -> PromptSpec:
    path = _path(name)
    mtime = _mtime(path, name)
    cached = _CACHE.get(name)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    compiled = _parse(_read_text(path), name)
    _CACHE[name] = (mtime, compiled)
    return compiled


def _parse(raw: str, name: str) -> PromptSpec:
    meta, body = _split_frontmatter(raw, name)

    unknown = set(meta) - _KNOWN_KEYS
    if unknown:
        msg = f"prompt {name!r} has unknown frontmatter keys: {sorted(unknown)}"
        raise PromptError(msg)

    variables = tuple(_str_list(meta, "variables", name))
    includes = tuple(_str_list(meta, "includes", name))
    overlap = set(variables) & set(includes)
    if overlap:
        msg = f"prompt {name!r} lists {sorted(overlap)} as both a variable and an include"
        raise PromptError(msg)

    system_text, user_text = _split_sections(body, name)
    return PromptSpec(
        system=_template(system_text, name) if system_text is not None else None,
        user=_template(user_text, name),
        max_tokens=_opt_int(meta, "max_tokens", name),
        temperature=_opt_float(meta, "temperature", name),
        variables=variables,
        includes=includes,
    )


def _split_frontmatter(raw: str, name: str) -> tuple[dict[str, Any], str]:
    lines = raw.split("\n")
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        msg = f"prompt {name!r} must open with a {_FRONTMATTER_DELIM} frontmatter block"
        raise PromptError(msg)

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONTMATTER_DELIM:
            try:
                meta = tomllib.loads("\n".join(lines[1:index]))
            except tomllib.TOMLDecodeError as exc:
                msg = f"prompt {name!r} has invalid TOML frontmatter: {exc}"
                raise PromptError(msg) from exc
            return meta, "\n".join(lines[index + 1 :])

    msg = f"prompt {name!r} has an unterminated frontmatter block"
    raise PromptError(msg)


def _split_sections(body: str, name: str) -> tuple[str | None, str]:
    """Split ``=== system ===`` / ``=== user ===``.

    The file boundary is one LLM call, not one message: ``max_tokens``,
    ``temperature`` and the variable contract belong to the call, and splitting
    a prompt across two files duplicates them and lets the halves desync — which
    is the very drift that put the strategist system prompt in two Python
    modules at once.
    """
    matches = list(_SECTION_RE.finditer(body))
    if not matches:
        return None, body.strip()

    if body[: matches[0].start()].strip():
        msg = f"prompt {name!r} has text before its first === section === marker"
        raise PromptError(msg)

    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        label = match.group(1)
        if label in sections:
            msg = f"prompt {name!r} repeats the === {label} === marker"
            raise PromptError(msg)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        # Strip the incidental newlines around a marker: a prompt's meaning must
        # not depend on how the file was laid out, and callers compose their own
        # separators between the pieces.
        sections[label] = body[match.end() : end].strip()

    if "user" not in sections:
        msg = f"prompt {name!r} has no === user === section"
        raise PromptError(msg)
    return sections.get("system"), sections["user"]


def _template(text: str, name: str) -> Template:
    template = Template(text)
    if not template.is_valid():
        msg = f"prompt {name!r} has a malformed placeholder (write a literal $ as $$)"
        raise PromptError(msg)
    return template


def _render(template: Template, mapping: dict[str, object], name: str) -> str:
    try:
        return template.substitute(mapping)
    except KeyError as exc:
        msg = f"prompt {name!r} uses undeclared placeholder ${exc.args[0]}"
        raise PromptError(msg) from exc


def _opt_int(meta: dict[str, Any], key: str, name: str) -> int | None:
    value = meta.get(key)
    if value is None:
        return None
    # bool is an int subclass; a max_tokens of True is a typo, not a value.
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"prompt {name!r} frontmatter {key} must be an integer, got {value!r}"
        raise PromptError(msg)
    return value


def _opt_float(meta: dict[str, Any], key: str, name: str) -> float | None:
    value = meta.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"prompt {name!r} frontmatter {key} must be a number, got {value!r}"
        raise PromptError(msg)
    return float(value)


def _str_list(meta: dict[str, Any], key: str, name: str) -> list[str]:
    value = meta.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"prompt {name!r} frontmatter {key} must be a list of strings, got {value!r}"
        raise PromptError(msg)
    return list(value)
