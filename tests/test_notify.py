"""Tests for the Telegram notification layer.

Two properties matter more than the wire format and are asserted hardest:
``notify`` never raises whatever Telegram does, and an ERROR produced *by* the
notifier never becomes another notification.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from options_m.config import Settings
from options_m.notify import (
    MESSAGE_LIMIT,
    NullNotifier,
    TelegramNotifier,
    build_notifier,
    escape,
    format_decision,
    format_error,
    format_order,
    format_summary,
    install_error_notifier,
    remove_error_notifier,
    truncate,
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "telegram_bot_token": "123:ABC",
        "telegram_chat_id": "-1001",
        "telegram_timeout_seconds": 0.5,
        "telegram_dedupe_seconds": 0,
    }
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Escaping and truncation
# ---------------------------------------------------------------------------


def test_escape_covers_every_markdown_v2_special() -> None:
    assert escape("a_b*c[d]") == "a\\_b\\*c\\[d\\]"
    assert escape("-3.7% (down)") == "\\-3\\.7% \\(down\\)"
    assert escape("back\\slash") == "back\\\\slash"


def test_escape_leaves_plain_text_alone() -> None:
    assert escape("SPY iron condor") == "SPY iron condor"


def test_truncate_is_a_noop_under_the_limit() -> None:
    assert truncate("short") == "short"


def test_truncate_clamps_to_the_telegram_limit() -> None:
    out = truncate("x" * (MESSAGE_LIMIT * 2))
    assert len(out) <= MESSAGE_LIMIT
    assert out.endswith("(truncated)")


def test_truncate_never_leaves_a_dangling_escape() -> None:
    # A cut landing between a backslash and the character it escapes would
    # make Telegram reject the whole message as malformed MarkdownV2.
    # Sized so the cut lands mid-escape: an odd number of backslashes survives
    # into the head unless truncate() trims the dangling one.
    head_len = MESSAGE_LIMIT - len("\n…(truncated)")
    text = "a" * (head_len - 1) + "\\" * 40
    out = truncate(text)
    body = out[: -len("\n…(truncated)")]
    trailing = len(body) - len(body.rstrip("\\"))
    assert trailing % 2 == 0


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def test_format_decision_tags_dry_run() -> None:
    text = format_decision(symbol="SPY", status="pending", dry_run=True, strategy="iron_condor")
    assert "\\[DRY RUN\\]" in text
    assert "iron\\_condor" in text


def test_format_decision_omits_the_tag_when_live() -> None:
    text = format_decision(symbol="SPY", status="pending", dry_run=False)
    assert "DRY RUN" not in text


def test_format_decision_carries_the_llm_reasoning() -> None:
    text = format_decision(
        symbol="NVDA",
        status="pending",
        dry_run=False,
        strategy="bull_put_spread",
        conviction=0.72,
        thesis="IV rank 82, range-bound",
        invalidation="close below 100",
        proposal_id=41,
    )
    assert "NVDA" in text
    assert "0\\.72" in text
    assert "IV rank 82, range\\-bound" in text
    assert "close below 100" in text
    assert "\\#41" in text


def test_format_decision_falls_back_for_an_unknown_status() -> None:
    assert "weird" in format_decision(symbol="SPY", status="weird", dry_run=False)


def test_format_order_reports_legs_and_status() -> None:
    text = format_order(
        action="open",
        underlying="SPY",
        status="submitted",
        dry_run=False,
        legs=["sell SPY260918P00600000", "buy SPY260918P00595000"],
        qty=2,
        limit_price="0.85",
        client_order_id="om-41",
    )
    assert "SPY260918P00600000" in text
    assert "om\\-41" in text
    assert "submitted" in text


def test_format_order_labels_a_close() -> None:
    text = format_order(action="close", underlying="SPY", status="close_submitted", dry_run=False)
    assert "Exit order" in text


def test_format_order_includes_the_error_text() -> None:
    text = format_order(
        action="open", underlying="SPY", status="failed", dry_run=False, error="insufficient bp"
    )
    assert "insufficient bp" in text


def test_format_summary_without_positions() -> None:
    text = format_summary(positions=[], account={"equity": 100000}, dry_run=False)
    assert "No open positions" in text


def test_format_summary_totals_unrealized_pl() -> None:
    text = format_summary(
        positions=[
            {"symbol": "SPY", "payload": {"market_value": 1200.5, "unrealized_pl": -45.2,
                                          "pnl_pct": -0.037}},
            {"symbol": "NVDA", "payload": {"market_value": 800.0, "unrealized_pl": 120.0,
                                           "pnl_pct": 0.176}},
        ],
        account={"equity": 100000, "cash": 50000},
        dry_run=False,
    )
    assert "SPY" in text and "NVDA" in text
    assert "74\\.80" in text  # -45.20 + 120.00
    assert "\\-3\\.7%" in text


def test_format_summary_tolerates_junk_numbers() -> None:
    text = format_summary(
        positions=[{"symbol": "SPY", "payload": {"market_value": None, "unrealized_pl": "n/a"}}],
        account=None,
        dry_run=False,
    )
    assert "SPY" in text


def test_format_error_includes_the_exception_but_not_the_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "options_m.agents", logging.ERROR, __file__, 1, "agent step failed", None,
            sys.exc_info(),
        )
    record.agent = "execution"
    text = format_error(record, dry_run=False)
    assert "agent step failed" in text
    assert "execution" in text
    assert "ValueError: boom" in text
    assert "Traceback" not in text


# ---------------------------------------------------------------------------
# build_notifier
# ---------------------------------------------------------------------------


def test_build_notifier_is_null_without_a_token() -> None:
    assert isinstance(build_notifier(Settings(telegram_bot_token=None)), NullNotifier)


def test_build_notifier_is_null_with_a_token_but_no_chat_id() -> None:
    settings = Settings(telegram_bot_token="123:ABC")
    assert isinstance(build_notifier(settings), NullNotifier)


def test_build_notifier_returns_a_telegram_notifier_when_configured() -> None:
    assert isinstance(build_notifier(_settings()), TelegramNotifier)


def test_null_notifier_swallows_everything() -> None:
    NullNotifier().notify("anything")  # must not raise


# ---------------------------------------------------------------------------
# TelegramNotifier delivery
# ---------------------------------------------------------------------------


class _Recorder:
    """A stub httpx transport that records every request body."""

    def __init__(self, *, status: int = 200, exc: Exception | None = None) -> None:
        self.status = status
        self.exc = exc
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        self.requests.append(json.loads(request.content))
        if self.exc is not None:
            raise self.exc
        return httpx.Response(self.status, json={"ok": self.status < 400})


def _client(recorder: _Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))


async def _flush(notifier: TelegramNotifier) -> None:
    """Wait for the drain task to finish everything queued so far."""
    await asyncio.wait_for(notifier._queue.join(), timeout=2.0)


async def test_notify_sends_the_message() -> None:
    recorder = _Recorder()
    async with TelegramNotifier(_settings(), client=_client(recorder)) as notifier:
        notifier.notify("hello")
        await _flush(notifier)
    assert recorder.requests[0]["text"] == "hello"
    assert recorder.requests[0]["chat_id"] == "-1001"
    assert recorder.requests[0]["parse_mode"] == "MarkdownV2"


async def test_notify_ignores_an_empty_message() -> None:
    recorder = _Recorder()
    async with TelegramNotifier(_settings(), client=_client(recorder)) as notifier:
        notifier.notify("")
        await _flush(notifier)
    assert recorder.requests == []


async def test_notify_does_not_raise_when_telegram_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = _Recorder(exc=httpx.ConnectTimeout("nope"))
    with caplog.at_level(logging.WARNING, logger="options_m.notify"):
        async with TelegramNotifier(_settings(), client=_client(recorder)) as notifier:
            notifier.notify("hello")  # must not raise
            await _flush(notifier)
    assert "telegram send failed" in caplog.text


async def test_a_send_failure_is_logged_at_warning_not_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # ERROR here would be fed straight back into the notifier by the error
    # bridge, producing an unbounded loop of failure notifications.
    recorder = _Recorder(exc=httpx.ConnectTimeout("nope"))
    with caplog.at_level(logging.DEBUG, logger="options_m.notify"):
        async with TelegramNotifier(_settings(), client=_client(recorder)) as notifier:
            notifier.notify("hello")
            await _flush(notifier)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_4xx_response_is_logged_and_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = _Recorder(status=400)
    with caplog.at_level(logging.WARNING, logger="options_m.notify"):
        async with TelegramNotifier(_settings(), client=_client(recorder)) as notifier:
            notifier.notify("hello")
            await _flush(notifier)
    assert "telegram rejected message" in caplog.text


async def test_the_queue_drops_the_oldest_when_full(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = _Recorder()
    notifier = TelegramNotifier(
        _settings(telegram_queue_max=2), client=_client(recorder)
    )
    # No drain task started, so nothing is consumed while we overfill it.
    with caplog.at_level(logging.WARNING, logger="options_m.notify"):
        for i in range(4):
            notifier.notify(f"m{i}")
    assert "telegram queue full" in caplog.text
    assert notifier._queue.qsize() == 2


async def test_duplicate_messages_are_suppressed_inside_the_window() -> None:
    recorder = _Recorder()
    settings = _settings(telegram_dedupe_seconds=60)
    async with TelegramNotifier(settings, client=_client(recorder)) as notifier:
        notifier.notify("same")
        notifier.notify("same")
        notifier.notify("different")
        await _flush(notifier)
    assert [r["text"] for r in recorder.requests] == ["same", "different"]


async def test_dedupe_is_off_when_the_window_is_zero() -> None:
    recorder = _Recorder()
    async with TelegramNotifier(_settings(), client=_client(recorder)) as notifier:
        notifier.notify("same")
        notifier.notify("same")
        await _flush(notifier)
    assert len(recorder.requests) == 2


async def test_start_is_idempotent() -> None:
    recorder = _Recorder()
    notifier = TelegramNotifier(_settings(), client=_client(recorder))
    await notifier.start()
    task = notifier._task
    await notifier.start()
    assert notifier._task is task
    await notifier.aclose()


async def test_aclose_without_start_is_safe() -> None:
    await TelegramNotifier(_settings(), client=_client(_Recorder())).aclose()


# ---------------------------------------------------------------------------
# ERROR bridge
# ---------------------------------------------------------------------------


class _Collector:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, text: str) -> None:
        self.messages.append(text)


@pytest.fixture
def bridge() -> Any:
    """Install the ERROR bridge onto a collector, and always remove it."""
    collector = _Collector()
    handler = install_error_notifier(collector, dry_run=False)
    yield collector
    remove_error_notifier(handler)


def test_error_logs_reach_the_notifier(bridge: _Collector) -> None:
    logging.getLogger("options_m.agents.execution").error("agent step failed")
    assert any("agent step failed" in m for m in bridge.messages)


def test_warnings_do_not_reach_the_notifier(bridge: _Collector) -> None:
    logging.getLogger("options_m.agents.execution").warning("just a warning")
    assert bridge.messages == []


def test_the_notifiers_own_errors_are_not_bridged(bridge: _Collector) -> None:
    # Otherwise a failing send logs an ERROR, which queues a message, which
    # fails to send, forever.
    logging.getLogger("options_m.notify").error("telegram send failed")
    assert bridge.messages == []


def test_install_error_notifier_is_a_noop_for_a_null_notifier() -> None:
    assert install_error_notifier(NullNotifier(), dry_run=False) is None


def test_install_error_notifier_does_not_stack_handlers() -> None:
    collector = _Collector()
    first = install_error_notifier(collector, dry_run=False)
    second = install_error_notifier(collector, dry_run=False)
    try:
        assert first is second
    finally:
        remove_error_notifier(first)


def test_remove_error_notifier_tolerates_none() -> None:
    remove_error_notifier(None)


def test_a_raising_notifier_never_breaks_logging() -> None:
    class _Exploding:
        def notify(self, text: str) -> None:
            raise RuntimeError("boom")

    handler = install_error_notifier(_Exploding(), dry_run=False)
    assert handler is not None
    handler.handleError = lambda record: None  # type: ignore[method-assign]
    try:
        logging.getLogger("options_m.agents").error("still logs")
    finally:
        remove_error_notifier(handler)
