"""TelegramLogger: fire-and-forget notifications to a Telegram group with topics."""

import logging
import os
from typing import NamedTuple

from .dedup import ErrorDeduplicator, fingerprint
from .formatting import (
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_MESSAGE_LIMIT,
    append_traceback,
    build_message,
    build_plain,
    format_traceback,
)
from .sender import TelegramSender

log = logging.getLogger("telegram_notifier")


class _Notification(NamedTuple):
    html: str
    plain: str
    thread_id: int | None
    traceback: str | None


class TelegramLogger:
    """Send service notifications to a Telegram group.

    All methods (sync and async variants) are fire-and-forget and never
    raise: notifications are queued in the sender and dispatched from a
    background worker thread, paced to stay under Telegram's per-group
    rate limit. blocking=True waits for the queue to drain after each
    call — meant for short-lived scripts, not for async apps (it blocks).

    When bot_token or chat_id is missing (arguments and environment both),
    the logger is disabled and every call is a no-op — safe for dev/tests.
    """

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: int | str | None = None,
        topics: dict[str, int] | None = None,
        service_name: str | None = None,
        environment: str | None = None,
        dedup_window_seconds: float = 300.0,
        blocking: bool = False,
        timeout: float = 10.0,
    ):
        bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.enabled = bool(bot_token and chat_id)
        self._chat_id = chat_id
        self._topics = topics or {}
        self._service_name = service_name
        self._environment = environment or os.environ.get("TELEGRAM_ENVIRONMENT")
        self._blocking = blocking
        self._dedup = ErrorDeduplicator(dedup_window_seconds)
        self._sender = TelegramSender(bot_token, timeout=timeout) if self.enabled else None

    # -- sync API ----------------------------------------------------------

    def info(self, text: str, **fields) -> None:
        self._dispatch(self._prepare("info", text, fields))

    def warning(self, text: str, **fields) -> None:
        self._dispatch(self._prepare("warning", text, fields))

    def user_action(self, text: str, **fields) -> None:
        self._dispatch(self._prepare("user_action", text, fields))

    def error(self, text: str, exc: BaseException | None = None, **fields) -> None:
        self._dispatch(self._prepare_error(text, exc, fields))

    # -- async API ---------------------------------------------------------

    async def ainfo(self, text: str, **fields) -> None:
        await self._asend(self._prepare("info", text, fields))

    async def awarning(self, text: str, **fields) -> None:
        await self._asend(self._prepare("warning", text, fields))

    async def auser_action(self, text: str, **fields) -> None:
        await self._asend(self._prepare("user_action", text, fields))

    async def aerror(self, text: str, exc: BaseException | None = None, **fields) -> None:
        await self._asend(self._prepare_error(text, exc, fields))

    # -- internals -----------------------------------------------------------

    def _prepare(self, level: str, text: str, fields: dict) -> _Notification | None:
        if not self.enabled:
            return None
        return _Notification(
            html=build_message(level, self._service_name, self._environment, text, fields),
            plain=build_plain(level, self._service_name, self._environment, text, fields),
            thread_id=self._topics.get(level),
            traceback=None,
        )

    def _prepare_error(self, text: str, exc: BaseException | None, fields: dict) -> _Notification | None:
        if not self.enabled:
            return None
        tb = None
        if exc is not None:
            should_send, suppressed = self._dedup.check(fingerprint(exc))
            if not should_send:
                return None
            if suppressed:
                window = int(self._dedup.window_seconds)
                text = f"{text} (×{suppressed} suppressed in last {window}s)"
            tb = format_traceback(exc)
        notification = self._prepare("error", text, fields)
        if tb is not None:
            notification = notification._replace(
                html=append_traceback(notification.html, tb),
                traceback=tb,
            )
        return notification

    def _dispatch(self, notification: _Notification | None) -> None:
        if notification is None:
            return
        self._send_safe(notification)
        if self._blocking:
            self._sender.flush()

    def _send_safe(self, notification: _Notification) -> None:
        try:
            self._send(notification)
        except Exception:
            log.exception("telegram notification failed")

    def _send(self, n: _Notification) -> None:
        if len(n.html) <= TELEGRAM_MESSAGE_LIMIT:
            self._sender.send_message(self._chat_id, n.html, n.thread_id)
        elif n.traceback is not None:
            self._sender.send_document(
                self._chat_id,
                "traceback.txt",
                n.traceback.encode(),
                caption=n.plain[:TELEGRAM_CAPTION_LIMIT],
                thread_id=n.thread_id,
            )
        else:
            self._sender.send_message(
                self._chat_id, n.plain[:TELEGRAM_MESSAGE_LIMIT], n.thread_id, parse_mode=None
            )

    async def _asend(self, notification: _Notification | None) -> None:
        # Enqueueing never blocks, so async variants share the sync path.
        self._dispatch(notification)