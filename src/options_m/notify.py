"""Outbound Telegram notifications.

One-way push only: the agents tell Telegram what they decided, submitted and
saw go wrong. Nothing here ever reads a command back.

Two rules shape the whole module.

**A notification must never fail a trading step.** ``notify`` is synchronous,
puts a formatted message on a bounded queue and returns. A background task
drains it. Every HTTP failure is swallowed and logged. If Telegram is down,
slow, or rate-limiting, the agents do not notice.

**A notification must never cause another notification.** ERROR records are
bridged into this notifier (see :func:`install_error_notifier`), so a send
failure logged at ERROR would queue a message describing the send failure,
forever. The bridge therefore ignores this module's own logger, and the drain
loop logs at WARNING rather than ERROR.

When the bot token or chat id is unset the process uses :class:`NullNotifier`,
mirroring how an unset ``DATABASE_URL`` means "run without a database".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from types import TracebackType
from typing import Any, Final, Protocol, runtime_checkable

import httpx

from options_m.config import Settings

logger = logging.getLogger(__name__)

# Telegram rejects a sendMessage body over 4096 characters outright.
MESSAGE_LIMIT: Final[int] = 4096
_TRUNCATION_SUFFIX: Final[str] = "\n…(kısaltıldı)"

# Every character MarkdownV2 treats as markup. Telegram rejects the whole
# message if any of these appears unescaped, including inside a plain word.
_MARKDOWN_V2_SPECIALS: Final[str] = "_*[]()~`>#+-=|{}.!\\"


def escape(text: str) -> str:
    """Escape ``text`` for Telegram's MarkdownV2 parse mode."""
    out: list[str] = []
    for char in text:
        if char in _MARKDOWN_V2_SPECIALS:
            out.append("\\")
        out.append(char)
    return "".join(out)


def truncate(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Clamp ``text`` to ``limit`` characters, marking that it was cut.

    Escaping happens before this, so the cut could land between a backslash and
    the character it escapes. Dropping a trailing lone backslash keeps the
    result parseable.
    """
    if len(text) <= limit:
        return text
    head = text[: limit - len(_TRUNCATION_SUFFIX)]
    # An odd number of trailing backslashes means the last one escapes nothing,
    # which Telegram rejects as malformed MarkdownV2.
    if (len(head) - len(head.rstrip("\\"))) % 2:
        head = head[:-1]
    return head + _TRUNCATION_SUFFIX


# ---------------------------------------------------------------------------
# Message formatting
#
# Pure functions, deliberately: the wire format is the part most worth
# asserting in tests, and it should be testable without a notifier, a queue or
# an HTTP client. Every one returns finished MarkdownV2 — callers pass the
# result straight to ``Notifier.notify``.
# ---------------------------------------------------------------------------


def _header(icon: str, title: str, *, dry_run: bool) -> str:
    """A bold title line, tagged when the process cannot actually trade."""
    tag = " \\[DRY RUN\\]" if dry_run else ""
    return f"{icon} *{escape(title)}*{tag}"


def _kv(label: str, value: object) -> str:
    return f"{escape(label)}: `{escape(str(value))}`"


def _pct(value: object) -> str:
    try:
        return f"{float(value):+.1%}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"


def _money(value: object) -> str:
    try:
        return f"${float(value):,.2f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"


def format_decision(
    *,
    symbol: str,
    status: str,
    dry_run: bool,
    strategy: str | None = None,
    conviction: float | None = None,
    thesis: str | None = None,
    invalidation: str | None = None,
    proposal_id: int | None = None,
    reason: str | None = None,
) -> str:
    """One StrategistAgent outcome: a proposal, a hold, a close, or an LLM failure."""
    icon, title = _DECISION_HEADERS.get(status, ("🤖", f"Karar: {status}"))
    lines = [_header(icon, f"{title} — {symbol}", dry_run=dry_run), ""]
    if strategy:
        lines.append(_kv("Strateji", strategy))
    if conviction is not None:
        lines.append(_kv("Conviction", f"{conviction:.2f}"))
    if reason:
        lines.append(_kv("Sebep", reason))
    if proposal_id is not None:
        lines.append(_kv("Proposal", f"#{proposal_id}"))
    if thesis:
        lines += ["", f"_{escape(thesis)}_"]
    if invalidation:
        lines.append(escape(f"Invalidation: {invalidation}"))
    return "\n".join(lines)


_DECISION_HEADERS: Final[dict[str, tuple[str, str]]] = {
    "pending": ("💡", "Yeni pozisyon önerisi"),
    "no_action": ("⏸", "Hold"),
    "close": ("🚪", "Kapatma kararı"),
    "llm_failed": ("⚠️", "LLM kararı üretemedi"),
}


def format_order(
    *,
    action: str,
    underlying: str,
    status: str,
    dry_run: bool,
    legs: list[str] | None = None,
    qty: int | None = None,
    limit_price: object = None,
    client_order_id: str | None = None,
    error: str | None = None,
) -> str:
    """One terminal order event: submitted, filled, failed, rejected, ambiguous."""
    icon = _ORDER_ICONS.get(status, "📄")
    verb = "Kapanış emri" if action == "close" else "Açılış emri"
    lines = [
        _header(icon, f"{verb} — {underlying}", dry_run=dry_run),
        "",
        _kv("Durum", status),
    ]
    if qty is not None:
        lines.append(_kv("Adet", qty))
    if limit_price is not None:
        lines.append(_kv("Limit", limit_price))
    if client_order_id:
        lines.append(_kv("Order id", client_order_id))
    if legs:
        lines.append("")
        lines += [f"• `{escape(leg)}`" for leg in legs]
    if error:
        lines += ["", escape(error)]
    return "\n".join(lines)


_ORDER_ICONS: Final[dict[str, str]] = {
    "submitted": "📤",
    "close_submitted": "📤",
    "filled": "✅",
    "partially_filled": "🟡",
    "failed": "❌",
    "rejected": "🚫",
    "broker_rejected": "🚫",
    "ambiguous": "❓",
}


def format_summary(
    *,
    positions: list[dict[str, Any]],
    account: dict[str, Any] | None,
    dry_run: bool,
    title: str = "Pozisyon özeti",
) -> str:
    """The periodic portfolio snapshot, read entirely from the local caches."""
    lines = [_header("📊", title, dry_run=dry_run), ""]
    if account:
        lines.append(_kv("Equity", _money(account.get("equity"))))
        lines.append(_kv("Nakit", _money(account.get("cash"))))
    lines.append(_kv("Açık pozisyon", len(positions)))

    if not positions:
        lines += ["", escape("Açık pozisyon yok.")]
        return "\n".join(lines)

    total_pl = 0.0
    lines.append("")
    for row in positions:
        payload: dict[str, Any] = row.get("payload") or {}
        with contextlib.suppress(TypeError, ValueError):
            total_pl += float(payload.get("unrealized_pl") or 0.0)
        lines.append(
            f"• *{escape(str(row.get('symbol', '?')))}* "
            f"{escape(_money(payload.get('market_value')))} "
            f"\\| {escape(_money(payload.get('unrealized_pl')))} "
            f"\\({escape(_pct(payload.get('pnl_pct')))}\\)"
        )
    lines += ["", _kv("Toplam gerçekleşmemiş P&L", _money(total_pl))]
    return "\n".join(lines)


def format_error(record: logging.LogRecord, *, dry_run: bool) -> str:
    """Render an ERROR log record as an alert.

    Only the message and the exception's first line are included. The full
    traceback belongs in the log stream, not in a chat window.
    """
    lines = [
        _header("🔴", "Hata", dry_run=dry_run),
        "",
        _kv("Logger", record.name),
        "",
        escape(record.getMessage()),
    ]
    if record.exc_info and record.exc_info[1] is not None:
        exc = record.exc_info[1]
        lines += ["", f"`{escape(f'{type(exc).__name__}: {exc}')}`"]
    agent = getattr(record, "agent", None)
    if agent:
        lines.append(_kv("Agent", agent))
    return "\n".join(lines)


@runtime_checkable
class Notifier(Protocol):
    """Fire-and-forget sink for operator-facing messages."""

    def notify(self, text: str) -> None:
        """Queue one pre-formatted MarkdownV2 message. Never raises."""


class NullNotifier:
    """A notifier that discards everything.

    The default everywhere a notifier is optional: the CLI, the tests, and any
    process started without Telegram credentials.
    """

    def notify(self, text: str) -> None:
        return None


class TelegramNotifier:
    """Queue-backed Telegram sender.

    Use as an async context manager so the drain task is started and stopped
    with the process::

        async with TelegramNotifier(settings) as notifier:
            ...
    """

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        token = (settings.telegram_bot_token or "").strip()
        self._chat_id = (settings.telegram_chat_id or "").strip()
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._timeout = settings.telegram_timeout_seconds
        self._dedupe_seconds = settings.telegram_dedupe_seconds
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.telegram_queue_max)
        self._client = client
        self._owns_client = client is None
        self._task: asyncio.Task[None] | None = None
        self._recent: dict[str, float] = {}
        self._dropped = 0


    async def __aenter__(self) -> TelegramNotifier:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def start(self) -> None:
        """Open the HTTP client and start draining the queue."""
        if self._task is not None:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        self._task = asyncio.create_task(self._drain(), name="telegram-notifier")
        logger.info("telegram notifier started", extra={"chat_id_set": bool(self._chat_id)})

    async def aclose(self) -> None:
        """Flush what is queued, then stop. Bounded by the send timeout."""
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=self._timeout)
            except TimeoutError:
                logger.warning(
                    "telegram queue not drained before shutdown",
                    extra={"pending": self._queue.qsize()},
                )
            task.cancel()
            # The drain task swallows send failures itself, so anything landing
            # here is the cancellation or a genuine bug; neither should keep the
            # process from shutting down.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def notify(self, text: str) -> None:
        """Queue one message. Synchronous, non-blocking, never raises."""
        if not text:
            return
        if self._is_duplicate(text):
            return
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            # The newest message is the one worth keeping: drop the oldest.
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:  # pragma: no cover - racy, harmless
                pass
            self._dropped += 1
            logger.warning(
                "telegram queue full; dropped oldest message",
                extra={"dropped_total": self._dropped},
            )
            with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover - racy
                self._queue.put_nowait(text)

    def _is_duplicate(self, text: str) -> bool:
        """True when ``text`` was already sent inside the dedupe window.

        An error storm repeats one message thousands of times; sending each is
        both useless and a fast route to Telegram's 429.
        """
        if self._dedupe_seconds <= 0:
            return False
        now = time.monotonic()
        cutoff = now - self._dedupe_seconds
        if len(self._recent) > 256:
            self._recent = {k: v for k, v in self._recent.items() if v > cutoff}
        last = self._recent.get(text)
        if last is not None and last > cutoff:
            return True
        self._recent[text] = now
        return False

    async def _drain(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self._send(text)
            except Exception as exc:
                # WARNING, not ERROR: the ERROR bridge would feed this back in.
                logger.warning(
                    "telegram send failed",
                    extra={"error": f"{type(exc).__name__}: {exc}"},
                )
            finally:
                self._queue.task_done()

    async def _send(self, text: str) -> None:
        if self._client is None:  # pragma: no cover - start() always sets it
            return
        response = await self._client.post(
            self._url,
            json={
                "chat_id": self._chat_id,
                "text": truncate(text),
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            },
        )
        if response.status_code >= 400:
            logger.warning(
                "telegram rejected message",
                extra={"status": response.status_code, "body": response.text[:500]},
            )


def build_notifier(settings: Settings) -> Notifier:
    """A configured :class:`TelegramNotifier`, or a null one when unset.

    The returned object is not started; the caller owns its lifecycle.
    """
    if not (settings.telegram_bot_token or "").strip():
        logger.info("telegram not configured; notifications disabled")
        return NullNotifier()
    if not (settings.telegram_chat_id or "").strip():
        logger.warning("TELEGRAM_BOT_TOKEN set without TELEGRAM_CHAT_ID; notifications disabled")
        return NullNotifier()
    return TelegramNotifier(settings)


# ---------------------------------------------------------------------------
# ERROR log bridge
# ---------------------------------------------------------------------------


class _ErrorNotifyHandler(logging.Handler):
    """Forward ERROR and above to a notifier, ignoring this module's own logs."""

    def __init__(self, notifier: Notifier, *, dry_run: bool) -> None:
        super().__init__(level=logging.ERROR)
        self._notifier = notifier
        self._dry_run = dry_run

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(__name__):
            return
        try:
            self._notifier.notify(format_error(record, dry_run=self._dry_run))
        except Exception:
            self.handleError(record)


def install_error_notifier(notifier: Notifier, *, dry_run: bool) -> logging.Handler | None:
    """Bridge root-logger ERRORs into ``notifier``. Returns the handler.

    Called after :func:`options_m.logging_config.setup_logging`, because
    dictConfig replaces the root handlers wholesale. Returns ``None`` for a
    :class:`NullNotifier`, so nothing is attached when Telegram is unconfigured.
    """
    if isinstance(notifier, NullNotifier):
        return None
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, _ErrorNotifyHandler):
            return existing
    handler = _ErrorNotifyHandler(notifier, dry_run=dry_run)
    root.addHandler(handler)
    return handler


def remove_error_notifier(handler: logging.Handler | None) -> None:
    """Detach a handler installed by :func:`install_error_notifier`."""
    if handler is not None:
        logging.getLogger().removeHandler(handler)
