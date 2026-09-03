"""Assembling the components the event loop runs.

:func:`load_profiles` reads ``PROFILES`` — an optional JSON list — into
:class:`Profile` objects; with it unset there is a single default profile taken
from the base :class:`~options_m.config.Settings`. :func:`assemble` turns each
profile into a :class:`Runner`: its own settings, broker session, store and
agent set, over one shared database pool.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from options_m.agents import Agent, build_agents
from options_m.config import Settings
from options_m.db import Database
from options_m.llm import FeatherlessLlm
from options_m.mcp_client import AlpacaMcp
from options_m.notify import Notifier, NullNotifier, ProfileNotifier
from options_m.store import Store

#: Profile name used when ``PROFILES`` is unset, and the schema default for the
#: ``account_id`` column. Do not change without a migration.
DEFAULT_PROFILE = "default"

#: A profile name is used verbatim as a row tag and a logger-child segment, so
#: keep it to a plain identifier.
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


class ProfileError(ValueError):
    """Raised at startup when ``PROFILES`` cannot be parsed.

    Fatal on purpose, like :func:`~options_m.mcp_client.assert_paper_intent`: a
    malformed value is a thing to fix, not to route around.
    """


@dataclass(frozen=True, slots=True)
class Profile:
    """One entry from ``PROFILES`` (or the implicit default)."""

    name: str
    alpaca_api_key: str | None
    alpaca_secret_key: str | None
    overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Runner:
    """A wired stack: settings, broker session, store, and agents."""

    name: str
    settings: Settings
    mcp: AlpacaMcp
    store: Store
    agents: list[Agent]


def load_profiles(settings: Settings) -> list[Profile]:
    """Parse ``PROFILES`` into profiles, or return the single implicit default.

    Raises:
        ProfileError: ``PROFILES`` is set but not a non-empty JSON list of
            ``{"name": ..., ...}`` objects with unique, identifier-shaped names.
    """
    raw = settings.profiles or os.environ.get("PROFILES")
    if raw is None or not raw.strip():
        return [
            Profile(
                name=DEFAULT_PROFILE,
                alpaca_api_key=settings.alpaca_api_key,
                alpaca_secret_key=settings.alpaca_secret_key,
            )
        ]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"PROFILES is not valid JSON: {exc}"
        raise ProfileError(msg) from exc

    if not isinstance(data, list) or not data:
        msg = "PROFILES must be a non-empty JSON list of objects"
        raise ProfileError(msg)

    out: list[Profile] = []
    seen: set[str] = set()
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            msg = f"PROFILES[{index}] must be an object"
            raise ProfileError(msg)

        name = entry.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            msg = (
                f"PROFILES[{index}].name must be a string matching "
                f"{_NAME_RE.pattern!r}, got {name!r}"
            )
            raise ProfileError(msg)
        if name in seen:
            msg = f"duplicate profile name {name!r}"
            raise ProfileError(msg)
        seen.add(name)

        overrides = entry.get("overrides", {})
        if not isinstance(overrides, dict):
            msg = f"PROFILES[{index}].overrides must be an object"
            raise ProfileError(msg)

        out.append(
            Profile(
                name=name,
                # An empty string reads the same as "not given".
                alpaca_api_key=entry.get("alpaca_api_key") or settings.alpaca_api_key,
                alpaca_secret_key=entry.get("alpaca_secret_key") or settings.alpaca_secret_key,
                overrides=dict(overrides),
            )
        )
    return out


def _settings_for(base: Settings, profile: Profile) -> Settings:
    """``base`` with the profile's credentials and overrides applied.

    ``model_copy(update=...)`` would skip validation, so this round-trips the
    merged dict through ``model_validate`` — an unknown key or an out-of-range
    value fails here, at startup.

    Raises:
        ProfileError: an override names a field ``Settings`` does not have.
        pydantic.ValidationError: an override value is the wrong type or range.
    """
    unknown = set(profile.overrides) - set(Settings.model_fields)
    if unknown:
        msg = f"profile {profile.name!r}: unknown override field(s) {sorted(unknown)}"
        raise ProfileError(msg)

    data = base.model_dump()
    data.update(profile.overrides)
    data["alpaca_api_key"] = profile.alpaca_api_key
    data["alpaca_secret_key"] = profile.alpaca_secret_key
    # model_validate on a BaseSettings validates the dict as a plain model; it
    # does not re-read the environment, which is what we want here.
    return Settings.model_validate(data)


async def assemble(
    base: Settings,
    db: Database,
    llm: FeatherlessLlm | None,
    notifier: Notifier | None,
    stack: AsyncExitStack,
) -> list[Runner]:
    """One :class:`Runner` per profile, all sharing ``db``.

    Each runner gets its own :class:`~options_m.mcp_client.AlpacaMcp` entered on
    ``stack`` and a :class:`~options_m.store.Store` tagged with the profile name;
    the agents are built by :func:`~options_m.agents.build_agents` unchanged.

    Past one profile every notification is wrapped in a
    :class:`~options_m.notify.ProfileNotifier`, so the single chat the profiles
    share stays readable. One profile is left exactly as it was — the same rule
    the log labels follow in ``__main__``.
    """
    profiles = load_profiles(base)
    many = len(profiles) > 1

    runners: list[Runner] = []
    for profile in profiles:
        settings = _settings_for(base, profile)
        mcp = await stack.enter_async_context(AlpacaMcp(settings))
        store = Store(db, account_id=profile.name)
        # A null sink is left bare: wrapped, it stops reading as "no Telegram
        # configured", and build_agents would register a reporter agent that
        # reports to nobody.
        sink = notifier
        if many and notifier is not None and not isinstance(notifier, NullNotifier):
            sink = ProfileNotifier(notifier, profile.name)
        agents = build_agents(settings, mcp, store, llm, notifier=sink)
        runners.append(Runner(profile.name, settings, mcp, store, agents))
    return runners
