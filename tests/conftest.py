"""Test-wide fixtures.

``Settings`` reads ``.env`` in the current working directory by design (so the
real service picks up local config with no extra flags) — but that means any
developer who happens to have a local ``.env`` file (for manual/local runs
against a real Postgres and Alpaca paper account) gets it silently loaded into
every test's ``Settings(...)`` too, even ones that explicitly pass
``database_url=None`` or otherwise assume defaults. That makes the test suite
non-hermetic: it passes or fails depending on files outside the repo. Disable
the ``.env`` file for the whole test session so tests only ever see what they
construct explicitly.
"""

from __future__ import annotations

import pytest

from options_m.config import Settings


@pytest.fixture(autouse=True, scope="session")
def _tests_never_read_a_local_dotenv() -> None:
    Settings.model_config["env_file"] = None
