"""Thin HTTP layer over the Telegram Bot API, sync and async."""

import asyncio
import logging
import time

import httpx

log = logging.getLogger("telegram_notifier")

API_BASE = "https://api.telegram.org"

# Cap on how long we honor Telegram's retry_after hint; anything longer
# means the group is flooded and dropping the message is the right call.
MAX_RETRY_AFTER = 30.0


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
    def __init__(self, bot_token: str, timeout: float = 10.0):
        self._base = f"{API_BASE}/bot{bot_token}"
        self._timeout = timeout

    # -- sync ------------------------------------------------------------

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        thread_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> None:
        self._post("/sendMessage", json=_message_payload(chat_id, text, thread_id, parse_mode))

    def send_document(
        self,
        chat_id: int | str,
        filename: str,
        content: bytes,
        caption: str | None = None,
        thread_id: int | None = None,
    ) -> None:
        data, files = _document_parts(chat_id, filename, content, caption, thread_id)
        self._post("/sendDocument", data=data, files=files)

    def _post(self, method: str, **kwargs) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(self._base + method, **kwargs)
            delay = _retry_delay(response)
            if delay is not None:
                time.sleep(delay)
                response = client.post(self._base + method, **kwargs)
            response.raise_for_status()

    # -- async -----------------------------------------------------------

    async def asend_message(
        self,
        chat_id: int | str,
        text: str,
        thread_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> None:
        await self._apost("/sendMessage", json=_message_payload(chat_id, text, thread_id, parse_mode))

    async def asend_document(
        self,
        chat_id: int | str,
        filename: str,
        content: bytes,
        caption: str | None = None,
        thread_id: int | None = None,
    ) -> None:
        data, files = _document_parts(chat_id, filename, content, caption, thread_id)
        await self._apost("/sendDocument", data=data, files=files)

    async def _apost(self, method: str, **kwargs) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(self._base + method, **kwargs)
            delay = _retry_delay(response)
            if delay is not None:
                await asyncio.sleep(delay)
                response = await client.post(self._base + method, **kwargs)
            response.raise_for_status()