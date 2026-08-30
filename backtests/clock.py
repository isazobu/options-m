"""Freeze the wall clock for an as-of replay.

``strategy_builder``, ``risk``, ``evidence`` and ``agents.execution`` all read
the current date straight from the stdlib rather than taking it as an argument
(``matrix.decide`` is the one exception — it already accepts ``as_of``). That is
fine in a live service and fatal in a replay: a contract expiring 2026-09-18 is
24 DTE on 25 August and 21 DTE on 28 August, and computing that against the real
today silently shifts every DTE filter, every Black-Scholes delta and every
expected move.

Rather than reach into production code for a backtest, this swaps the ``date``
and ``datetime`` names bound in those modules for subclasses whose ``today()`` /
``now()`` answer the replay date. Everything else about them is unchanged, so
``date.fromisoformat`` and arithmetic behave exactly as before.

This is a workaround, not an endorsement: the hidden clock dependency is worth
replacing with an explicit ``as_of`` parameter, the way ``matrix.decide``
already does it.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType
from typing import Any

# Modules that read the clock through a module-level ``date`` / ``datetime``.
_PATCH_TARGETS = (
    "options_m.strategy_builder",
    "options_m.risk",
    "options_m.evidence.evidence",
    "options_m.agents.execution",
)


def _frozen_types(as_of: _dt.date) -> tuple[type, type]:
    moment = _dt.datetime.combine(as_of, _dt.time(20, 0), tzinfo=_dt.UTC)

    class FrozenDate(_dt.date):
        @classmethod
        def today(cls) -> _dt.date:
            return as_of

    class FrozenDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> _dt.datetime:
            return moment if tz else moment.replace(tzinfo=None)

        @classmethod
        def utcnow(cls) -> _dt.datetime:
            return moment.replace(tzinfo=None)

    return FrozenDate, FrozenDatetime


@contextmanager
def frozen_at(as_of: _dt.date) -> Iterator[None]:
    """Run the block as if today were ``as_of``."""
    import importlib

    frozen_date, frozen_datetime = _frozen_types(as_of)
    saved: list[tuple[ModuleType, str, Any]] = []
    for name in _PATCH_TARGETS:
        module = importlib.import_module(name)
        for attr, replacement in (("date", frozen_date), ("datetime", frozen_datetime)):
            if hasattr(module, attr):
                saved.append((module, attr, getattr(module, attr)))
                setattr(module, attr, replacement)
    try:
        yield
    finally:
        for module, attr, original in reversed(saved):
            setattr(module, attr, original)
