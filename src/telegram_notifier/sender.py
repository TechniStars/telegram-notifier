"""Thin HTTP layer over the Telegram Bot API with a rate-limited send queue."""

import atexit
import logging
import queue
import threading
import time

import httpx

from .formatting import TELEGRAM_MESSAGE_LIMIT

log = logging.getLogger("telegram_notifier")

API_BASE = "https://api.telegram.org"

# Cap on how long we honor Telegram's retry_after hint; anything longer
# means the group is flooded and dropping the message is the right call.
MAX_RETRY_AFTER = 30.0

# Telegram allows ~20 messages per minute to the same group; pacing sends
# this far apart keeps bursts under that limit across all topics.
DEFAULT_MIN_INTERVAL = 3.0

DEFAULT_MAX_QUEUE_SIZE = 500

MAX_ATTEMPTS = 3

# How long the atexit hook waits for the queue to drain, so short-lived
# scripts get their messages out without a flooded queue stalling exit.
EXIT_FLUSH_TIMEOUT = 10.0


def _message_payload(
    chat_id: int | str,
    text: str,
    thread_id: int | None,
    parse_mode: str | None,
) -> dict:
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    return payload


def _document_parts(
    chat_id: int | str,
    filename: str,
    content: bytes,
    caption: str | None,
    thread_id: int | None,
) -> tuple[dict, dict]:
    data: dict = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if thread_id is not None:
        data["message_thread_id"] = str(thread_id)
    files = {"document": (filename, content)}
    return data, files


def _retry_delay(response: httpx.Response) -> float | None:
    """Return a sleep-then-retry delay for retryable responses, else None."""
    if response.status_code == 429:
        try:
            delay = float(response.json()["parameters"]["retry_after"])
        except Exception:
            delay = 3.0
        return min(delay, MAX_RETRY_AFTER)
    if response.status_code >= 500:
        return 1.0
    return None


class TelegramSender:
    """Rate-limited sender: sends are queued and dispatched from a daemon
    worker thread, paced ``min_interval`` seconds apart.

    Callers never block on network I/O. When the queue is full new messages
    are dropped and the drop count is appended to the next message that goes
    out. Use :meth:`flush` to wait for the queue to drain.
    """

    def __init__(
        self,
        bot_token: str,
        timeout: float = 10.0,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE,
    ):
        self._base = f"{API_BASE}/bot{bot_token}"
        self._timeout = timeout
        self._min_interval = min_interval
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self._lock = threading.Lock()
        self._pending = 0  # queued + in-flight, guarded by _lock
        self._dropped = 0  # guarded by _lock
        self._worker_started = False

    # -- sync ------------------------------------------------------------

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        thread_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> None:
        self._enqueue("/sendMessage", {"json": _message_payload(chat_id, text, thread_id, parse_mode)})

    def send_document(
        self,
        chat_id: int | str,
        filename: str,
        content: bytes,
        caption: str | None = None,
        thread_id: int | None = None,
    ) -> None:
        data, files = _document_parts(chat_id, filename, content, caption, thread_id)
        self._enqueue("/sendDocument", {"data": data, "files": files})

    def flush(self, timeout: float = 60.0) -> None:
        """Block until all queued messages are sent or the timeout elapses."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._pending == 0:
                    return
            time.sleep(0.05)

    # -- async -----------------------------------------------------------
    # Enqueueing never blocks, so the async variants share the sync path;
    # they stay async for backward compatibility of the public API.

    async def asend_message(
        self,
        chat_id: int | str,
        text: str,
        thread_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> None:
        self.send_message(chat_id, text, thread_id, parse_mode)

    async def asend_document(
        self,
        chat_id: int | str,
        filename: str,
        content: bytes,
        caption: str | None = None,
        thread_id: int | None = None,
    ) -> None:
        self.send_document(chat_id, filename, content, caption, thread_id)

    # -- internals ---------------------------------------------------------

    def _enqueue(self, method: str, kwargs: dict) -> None:
        self._ensure_worker()
        with self._lock:
            if self._queue.full():
                self._dropped += 1
                log.warning("telegram queue full, message dropped")
                return
            self._queue.put_nowait((method, kwargs))
            self._pending += 1

    def _ensure_worker(self) -> None:
        if self._worker_started:
            return
        with self._lock:
            if self._worker_started:
                return
            threading.Thread(target=self._worker_loop, name="telegram-sender", daemon=True).start()
            atexit.register(self.flush, EXIT_FLUSH_TIMEOUT)
            self._worker_started = True

    def _worker_loop(self) -> None:
        while True:
            method, kwargs = self._queue.get()
            try:
                self._post(method, **self._with_dropped_note(method, kwargs))
            except Exception:
                log.exception("telegram send failed")
            finally:
                with self._lock:
                    self._pending -= 1
            time.sleep(self._min_interval)

    def _with_dropped_note(self, method: str, kwargs: dict) -> dict:
        """Attach the count of dropped messages to the next outgoing message."""
        if method != "/sendMessage":
            return kwargs
        with self._lock:
            dropped, self._dropped = self._dropped, 0
        if not dropped:
            return kwargs
        payload = dict(kwargs["json"])
        note = f"\n(+{dropped} dropped: queue full)"
        if len(payload["text"]) + len(note) > TELEGRAM_MESSAGE_LIMIT:
            # No room in this message; carry the count over to the next one.
            with self._lock:
                self._dropped += dropped
            return kwargs
        payload["text"] += note
        return {**kwargs, "json": payload}

    def _post(self, method: str, **kwargs) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            for attempt in range(MAX_ATTEMPTS):
                response = client.post(self._base + method, **kwargs)
                delay = _retry_delay(response)
                if delay is None:
                    break
                if attempt < MAX_ATTEMPTS - 1:
                    time.sleep(delay)
            response.raise_for_status()