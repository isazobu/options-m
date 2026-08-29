from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from options_m import logging_config
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


# ---------------------------------------------------------------------------
# FastMCP null-payload noise filter
# ---------------------------------------------------------------------------

_NOISE = (
    "[Client-82de] Error parsing structured content: 1 validation error for "
    "dict[str,any]\n  Input should be a valid dictionary "
    "[type=dict_type, input_value=None, input_type=NoneType]"
)
_REAL_MISMATCH = (
    "[Client-82de] Error parsing structured content: 1 validation error for "
    "dict[str,any]\n  Input should be a valid dictionary "
    "[type=dict_type, input_value='oops', input_type=str]"
)


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name=logging_config._FASTMCP_TOOL_LOGGER,
        level=logging.ERROR,
        pathname="tools.py",
        lineno=467,
        msg=message,
        args=None,
        exc_info=None,
    )


def test_the_first_parse_error_is_never_suppressed() -> None:
    """Nothing is hidden: the condition still announces itself, at ERROR."""
    noise_filter = logging_config._RepeatedParseErrorFilter()

    assert noise_filter.filter(_record(_NOISE)) is True


def test_identical_repeats_are_dropped() -> None:
    """One line says the condition exists; the ten-thousandth adds nothing."""
    noise_filter = logging_config._RepeatedParseErrorFilter()
    noise_filter.filter(_record(_NOISE))

    assert noise_filter.filter(_record(_NOISE)) is False
    assert noise_filter.filter(_record(_NOISE)) is False


def test_a_different_parse_error_gets_its_own_first_occurrence() -> None:
    """A real schema mismatch must not be swallowed by an unrelated repeat."""
    noise_filter = logging_config._RepeatedParseErrorFilter()
    noise_filter.filter(_record(_NOISE))

    assert noise_filter.filter(_record(_REAL_MISMATCH)) is True


def test_unrelated_messages_from_the_same_logger_are_never_deduplicated() -> None:
    """Only parse errors are rate-limited; everything else passes every time."""
    noise_filter = logging_config._RepeatedParseErrorFilter()
    other = "connection to the tool server was lost"

    assert noise_filter.filter(_record(other)) is True
    assert noise_filter.filter(_record(other)) is True


def test_installing_the_filter_twice_does_not_stack_it() -> None:
    logger = logging.getLogger(logging_config._FASTMCP_TOOL_LOGGER)
    logger.filters = [
        f for f in logger.filters if not isinstance(f, logging_config._RepeatedParseErrorFilter)
    ]

    logging_config.install_mcp_noise_filter()
    logging_config.install_mcp_noise_filter()

    installed = [
        f for f in logger.filters if isinstance(f, logging_config._RepeatedParseErrorFilter)
    ]
    assert len(installed) == 1
