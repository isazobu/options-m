from __future__ import annotations

from contextlib import AsyncExitStack

import pytest
from pydantic import ValidationError

from options_m.agents import Agent
from options_m.config import Settings
from options_m.db import Database
from options_m.notify import NullNotifier, ProfileNotifier
from options_m.runtime import (
    DEFAULT_PROFILE,
    Profile,
    ProfileError,
    _settings_for,
    assemble,
    load_profiles,
)


def _base() -> Settings:
    return Settings(
        database_url=None,
        alpaca_api_key="BASEKEY",  # noqa: S106
        alpaca_secret_key="BASESECRET",  # noqa: S106
    )


def test_unset_yields_one_default_from_base_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROFILES", raising=False)
    profiles = load_profiles(_base())
    assert len(profiles) == 1
    only = profiles[0]
    assert only.name == DEFAULT_PROFILE
    assert only.alpaca_api_key == "BASEKEY"
    assert only.alpaca_secret_key == "BASESECRET"
    assert only.overrides == {}


def test_blank_env_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILES", "   ")
    assert [p.name for p in load_profiles(_base())] == [DEFAULT_PROFILE]


def test_the_settings_field_is_read_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value from .env lands on Settings.profiles, not os.environ."""
    monkeypatch.delenv("PROFILES", raising=False)
    settings = Settings(
        database_url=None,
        alpaca_api_key="BASEKEY",  # noqa: S106
        alpaca_secret_key="BASESECRET",  # noqa: S106
        profiles='[{"name":"a"},{"name":"b"}]',
    )
    assert [p.name for p in load_profiles(settings)] == ["a", "b"]


def test_json_list_becomes_n_profiles_with_key_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "PROFILES",
        '[{"name":"a","alpaca_api_key":"K1","alpaca_secret_key":"S1"},'
        '{"name":"b","overrides":{"base_risk_pct_per_trade":0.03}}]',
    )
    profiles = load_profiles(_base())
    assert [p.name for p in profiles] == ["a", "b"]
    assert (profiles[0].alpaca_api_key, profiles[0].alpaca_secret_key) == ("K1", "S1")
    # b gave no keys -> falls back to the base credentials.
    assert (profiles[1].alpaca_api_key, profiles[1].alpaca_secret_key) == ("BASEKEY", "BASESECRET")
    assert profiles[1].overrides == {"base_risk_pct_per_trade": 0.03}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",  # object, not a list
        "[]",  # empty list
        "[1, 2]",  # not objects
        '[{"alpaca_api_key":"x"}]',  # missing name
        '[{"name":"Bad Name"}]',  # name fails the identifier regex
        '[{"name":"a"},{"name":"a"}]',  # duplicate name
        '[{"name":"a","overrides":[]}]',  # overrides not an object
    ],
)
def test_malformed_profiles_raise(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("PROFILES", raw)
    with pytest.raises(ProfileError):
        load_profiles(_base())


def test_settings_for_applies_overrides_and_credentials() -> None:
    base = _base()
    profile = Profile(
        name="b",
        alpaca_api_key="K2",  # noqa: S106
        alpaca_secret_key="S2",  # noqa: S106
        overrides={"base_risk_pct_per_trade": 0.03, "dte_target_max": 21},
    )
    derived = _settings_for(base, profile)

    assert derived.alpaca_api_key == "K2"
    assert derived.alpaca_secret_key == "S2"
    assert derived.base_risk_pct_per_trade == pytest.approx(0.03)
    assert derived.dte_target_max == 21
    # Untouched fields are inherited from the base.
    assert derived.universe == base.universe
    # The base object is not mutated.
    assert base.base_risk_pct_per_trade != pytest.approx(0.03)


def test_settings_for_rejects_an_unknown_override() -> None:
    profile = Profile("x", "k", "s", {"not_a_real_setting": 1})  # noqa: S106
    with pytest.raises(ProfileError):
        _settings_for(_base(), profile)


def test_settings_for_rejects_an_out_of_range_override() -> None:
    # base_risk_pct_per_trade is constrained to (0, 1].
    profile = Profile("x", "k", "s", {"base_risk_pct_per_trade": 5.0})  # noqa: S106
    with pytest.raises(ValidationError):
        _settings_for(_base(), profile)


class _Collector:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, text: str) -> None:
        self.messages.append(text)


def _offline(**overrides: object) -> Settings:
    """A base with nothing external configured, so nothing is opened or spawned."""
    return Settings(
        database_url=None,
        alpaca_api_key=None,
        alpaca_secret_key=None,
        telegram_bot_token=None,
        **overrides,  # type: ignore[arg-type]
    )


def _sink_of(agents: list[Agent]) -> object:
    """The notifier the reporter agent was handed."""
    reporter = next(agent for agent in agents if agent.name == "telegram_reporter")
    return reporter._notifier  # type: ignore[attr-defined]


async def test_each_profile_notifies_under_its_own_name() -> None:
    collector = _Collector()
    base = _offline(profiles='[{"name":"isazobu"},{"name":"rukiyeaslan"}]')
    async with AsyncExitStack() as stack:
        runners = await assemble(base, Database(base), None, collector, stack)

    for runner in runners:
        sink = _sink_of(runner.agents)
        assert isinstance(sink, ProfileNotifier)
        sink.notify("📊 *Portfolio snapshot*")
    assert collector.messages == [
        "👤 *isazobu*\n📊 *Portfolio snapshot*",
        "👤 *rukiyeaslan*\n📊 *Portfolio snapshot*",
    ]


async def test_profiles_without_telegram_register_no_reporter() -> None:
    """A wrapped null sink would read as configured and start a reporter agent."""
    base = _offline(profiles='[{"name":"isazobu"},{"name":"rukiyeaslan"}]')
    async with AsyncExitStack() as stack:
        runners = await assemble(base, Database(base), None, NullNotifier(), stack)

    for runner in runners:
        assert [agent.name for agent in runner.agents if agent.name == "telegram_reporter"] == []


async def test_a_single_profile_is_not_tagged() -> None:
    """One account is unambiguous, so its messages read exactly as they did."""
    collector = _Collector()
    base = _offline()
    async with AsyncExitStack() as stack:
        runners = await assemble(base, Database(base), None, collector, stack)

    assert _sink_of(runners[0].agents) is collector
