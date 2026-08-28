from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from options_m.logging_config import setup_logging


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


def test_json_output_contains_core_fields(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", fmt="json")
    logging.getLogger("test").info("hello", extra={"request_id": "abc"})

    record = json.loads(capsys.readouterr().out.strip())
    assert record["level"] == "INFO"
    assert record["logger"] == "test"
    assert record["message"] == "hello"
    assert record["request_id"] == "abc"
    assert "timestamp" in record


def test_exception_is_serialized(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("ERROR", fmt="json")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("test").exception("failed")

    record = json.loads(capsys.readouterr().out.strip())
    assert "ValueError: boom" in record["exception"]


def test_level_filters_lower_records(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("WARNING", fmt="text")
    logging.getLogger("test").info("suppressed")
    assert capsys.readouterr().out == ""
