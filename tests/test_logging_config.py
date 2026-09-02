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


# ---------------------------------------------------------------------------
# Bot-token redaction
#
# Telegram puts the bot token in the URL path, and httpx logs whole URLs at
# INFO, so every notification used to print the token into the log stream.
# ---------------------------------------------------------------------------

_FAKE_BOT = "123456789:FAKE-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_TELEGRAM_URL = f"https://api.telegram.org/bot{_FAKE_BOT}/sendMessage"


def test_httpx_request_line_does_not_print_the_bot_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exact leak seen in production: one INFO line per notification."""
    setup_logging("INFO", fmt="json")
    logging.getLogger("httpx").info('HTTP Request: POST %s "HTTP/1.1 200 OK"', _TELEGRAM_URL)

    out = capsys.readouterr().out
    assert _FAKE_BOT not in out
    assert "api.telegram.org/bot<redacted>/sendMessage" in out


def test_text_format_redacts_the_token_too(capsys: pytest.CaptureFixture[str]) -> None:
    """LOG_FORMAT=text must not be a way around the redaction."""
    setup_logging("INFO", fmt="text")
    logging.getLogger("httpx").info("HTTP Request: POST %s", _TELEGRAM_URL)

    out = capsys.readouterr().out
    assert _FAKE_BOT not in out
    assert "bot<redacted>" in out


def test_a_traceback_carrying_the_url_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    """httpx errors quote the request URL, and notify logs the exception text."""
    setup_logging("ERROR", fmt="json")
    try:
        raise RuntimeError(f"connect error for url '{_TELEGRAM_URL}'")
    except RuntimeError:
        logging.getLogger("options_m.notify").exception("telegram send failed")

    record = json.loads(capsys.readouterr().out.strip())
    assert _FAKE_BOT not in json.dumps(record)
    assert "bot<redacted>" in record["exception"]


def test_extra_fields_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    """notify.py passes the exception through extra={"error": ...}."""
    setup_logging("WARNING", fmt="json")
    logging.getLogger("options_m.notify").warning(
        "telegram send failed", extra={"error": f"ConnectError: {_TELEGRAM_URL}"}
    )

    out = capsys.readouterr().out
    assert _FAKE_BOT not in out
    assert "bot<redacted>" in out


def test_unrelated_urls_are_left_alone(capsys: pytest.CaptureFixture[str]) -> None:
    """Redaction must not blind the useful half of the httpx request log."""
    setup_logging("INFO", fmt="json")
    logging.getLogger("httpx").info(
        'HTTP Request: POST https://api.featherless.ai/v1/chat/completions "HTTP/1.1 200 OK"'
    )

    record = json.loads(capsys.readouterr().out.strip())
    assert record["message"] == (
        'HTTP Request: POST https://api.featherless.ai/v1/chat/completions "HTTP/1.1 200 OK"'
    )


def test_redact_secrets_is_a_no_op_for_ordinary_text() -> None:
    assert logging_config.redact_secrets("position pulse open_legs=8") == (
        "position pulse open_legs=8"
    )
