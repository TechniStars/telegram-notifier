import asyncio
import logging
from unittest.mock import MagicMock

import httpx
import pytest

from telegram_notifier import TelegramLogger
from telegram_notifier.dedup import ErrorDeduplicator, fingerprint
from telegram_notifier import sender as sender_module
from telegram_notifier.sender import MAX_RETRY_AFTER, TelegramSender, TelegramSendError, _retry_delay, redact_token, scrub_secrets
from telegram_notifier.formatting import (
    TELEGRAM_MESSAGE_LIMIT,
    append_traceback,
    build_message,
    format_traceback,
)


def make_logger(**kwargs) -> TelegramLogger:
    topics = kwargs.pop("topics", {"info": 2, "error": 7, "user_action": 10})
    tg = TelegramLogger(
        bot_token="123:abc",
        chat_id=-100123,
        topics=topics,
        service_name="test-svc",
        environment="test-env",
        blocking=True,
        **kwargs,
    )
    tg._sender = MagicMock()
    return tg


def raise_and_catch() -> Exception:
    try:
        raise ValueError("boom <&>")
    except ValueError as e:
        return e


class TestFormatting:
    def test_build_message_escapes_html(self):
        msg = build_message("info", "svc <x>", None, "a <b> & c", {"user_id": 1})
        assert "<x>" not in msg.replace("&lt;x&gt;", "")
        assert "&lt;b&gt;" in msg
        assert "user_id: <code>1</code>" in msg

    def test_environment_in_origin_line(self):
        msg = build_message("info", "svc", "prod", "hello", {})
        assert "<b>hello</b>" in msg
        assert "<i>[svc · prod]</i>" in msg

    def test_fields_one_per_line(self):
        msg = build_message("info", None, None, "t", {"a": 1, "b": "x"})
        assert "\n\na: <code>1</code>\nb: <code>x</code>" in msg

    def test_traceback_trimmed_keeps_tail(self):
        exc = raise_and_catch()
        tb = format_traceback(exc, budget=50)
        assert tb.startswith("…\n")
        assert len(tb) <= 52
        assert "boom" in tb  # cause is at the tail

    def test_expandable_blockquote(self):
        out = append_traceback("header", "tb <line>")
        assert "<blockquote expandable><pre>tb &lt;line&gt;</pre></blockquote>" in out


class TestDedup:
    def test_first_send_then_suppress(self):
        d = ErrorDeduplicator(window_seconds=300)
        assert d.check("k") == (True, 0)
        assert d.check("k") == (False, 0)
        assert d.check("k") == (False, 0)

    def test_window_expiry_reports_count(self, monkeypatch):
        d = ErrorDeduplicator(window_seconds=300)
        d.check("k")
        d.check("k")
        d.check("k")
        entry = d._seen["k"]
        d._seen["k"] = (entry[0] - 301, entry[1])
        assert d.check("k") == (True, 2)

    def test_fingerprint_distinguishes_types(self):
        e1 = raise_and_catch()
        try:
            raise KeyError("x")
        except KeyError as e:
            e2 = e
        assert fingerprint(e1) != fingerprint(e2)
        assert fingerprint(e1) == fingerprint(raise_and_catch())


class TestLogger:
    def test_disabled_without_credentials(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        tg = TelegramLogger()
        assert tg.enabled is False
        tg.info("noop")  # must not raise
        tg.error("noop", exc=raise_and_catch())

    def test_info_routes_to_topic(self):
        tg = make_logger()
        tg.info("worker started", queue="agentic")
        (chat_id, text, thread_id), _ = tg._sender.send_message.call_args
        assert chat_id == -100123
        assert thread_id == 2
        assert "[test-svc · test-env]" in text
        assert "queue: <code>agentic</code>" in text

    def test_other_routes_by_kind(self):
        tg = make_logger(topics={"info": 2, "error": 7, "reports": 9})
        tg.other("reports", "content report", object_id="recipe-1")
        (chat_id, text, thread_id), _ = tg._sender.send_message.call_args
        assert chat_id == -100123
        assert thread_id == 9
        assert "<b>content report</b>" in text
        assert "object_id: <code>recipe-1</code>" in text

    def test_html_message_routes_custom_markup(self):
        tg = make_logger(topics={"info": 2, "user_feedbacks": 1806})
        tg.html_message(
            "user_feedbacks",
            "daily_feedback_submission",
            "user_id: 123\n1. <b>Question</b>\n<i>Answer</i>",
            "user_id: 123\n1. Question\nAnswer",
        )
        (chat_id, text, thread_id), _ = tg._sender.send_message.call_args
        assert chat_id == -100123
        assert thread_id == 1806
        assert "user_id: 123" in text
        assert "1. <b>Question</b>" in text
        assert "<i>Answer</i>" in text

    def test_oversized_html_message_uses_plain_fallback(self):
        tg = make_logger()
        tg.html_message(
            "info",
            "custom",
            "x" * (TELEGRAM_MESSAGE_LIMIT + 1),
            "plain fallback",
        )
        (chat_id, text, thread_id), kwargs = tg._sender.send_message.call_args
        assert chat_id == -100123
        assert thread_id == 2
        assert text.endswith("plain fallback")
        assert kwargs["parse_mode"] is None

    def test_other_routes_user_action_kind(self):
        tg = make_logger()
        tg.other("user_action", "user_registered", user_id=1)
        (_, _, thread_id), _ = tg._sender.send_message.call_args
        assert thread_id == 10

    def test_error_with_traceback(self):
        tg = make_logger()
        tg.error("failed", exc=raise_and_catch(), user_id=5)
        (_, text, thread_id), _ = tg._sender.send_message.call_args
        assert thread_id == 7
        assert "<blockquote expandable>" in text
        assert "ValueError" in text

    def test_error_dedup_suppresses_repeat(self):
        tg = make_logger()
        tg.error("failed", exc=raise_and_catch())
        tg.error("failed", exc=raise_and_catch())
        assert tg._sender.send_message.call_count == 1

    def test_oversized_traceback_falls_back_to_document(self):
        tg = make_logger()
        exc = raise_and_catch()
        tg._dedup = ErrorDeduplicator()
        long_text = "x" * 100
        # force oversized html by patching format budget via huge fields
        tg.error(long_text, exc=exc, blob="y" * (TELEGRAM_MESSAGE_LIMIT))
        assert tg._sender.send_document.called
        assert not tg._sender.send_message.called

    def test_sender_exception_swallowed(self):
        tg = make_logger()
        tg._sender.send_message.side_effect = RuntimeError("network down")
        tg.info("still fine")  # must not raise

    def test_async_api(self):
        tg = make_logger()

        async def run():
            await tg.ainfo("hello", user_id=1)
            await tg.aother("reports", "report", object_id=2)
            await tg.ahtml_message("reports", "custom", "<b>custom</b>", "custom")
            await tg.aerror("bad", exc=raise_and_catch())

        asyncio.run(run())
        assert tg._sender.send_message.call_count == 4


class TestSenderQueue:
    def make_sender(self, **kwargs) -> TelegramSender:
        sender = TelegramSender("123:abc", min_interval=0.0, **kwargs)
        sender.posts = []
        sender._post = lambda method, **kw: sender.posts.append((method, kw))
        return sender

    def test_worker_sends_queued_message(self):
        sender = self.make_sender()
        sender.send_message(-100123, "hi", thread_id=2)
        sender.flush(timeout=5.0)
        assert sender.posts == [
            (
                "/sendMessage",
                {"json": {"chat_id": -100123, "text": "hi", "parse_mode": "HTML", "message_thread_id": 2}},
            )
        ]

    def test_full_queue_drops_and_reports_count(self):
        sender = self.make_sender(max_queue_size=1)
        sender._ensure_worker = lambda: None  # keep worker off so the queue stays full
        sender.send_message(-1, "first")
        sender.send_message(-1, "dropped 1")
        sender.send_message(-1, "dropped 2")
        assert sender._queue.qsize() == 1
        assert sender._dropped == 2
        method, kwargs = sender._queue.get()
        noted = sender._with_dropped_note(method, kwargs)
        assert noted["json"]["text"] == "first\n(+2 dropped: queue full)"
        assert sender._dropped == 0

    def test_document_does_not_consume_dropped_count(self):
        sender = self.make_sender()
        sender._dropped = 3
        kwargs = {"data": {"chat_id": "-1"}, "files": {"document": ("t.txt", b"x")}}
        assert sender._with_dropped_note("/sendDocument", kwargs) is kwargs
        assert sender._dropped == 3

    def test_retry_delay_parses_telegram_hint(self):
        response = MagicMock(status_code=429)
        response.json.return_value = {"parameters": {"retry_after": 7}}
        assert _retry_delay(response) == 7.0
        response.json.return_value = {"parameters": {"retry_after": 999}}
        assert _retry_delay(response) == MAX_RETRY_AFTER
        assert _retry_delay(MagicMock(status_code=502)) == 1.0
        assert _retry_delay(MagicMock(status_code=200)) is None


class TestBotTokenRedaction:
    TOKEN = "123456789:AAH-secret_Token-value"

    def _sender_with_transport(self, monkeypatch, handler):
        real_client = httpx.Client
        monkeypatch.setattr(
            sender_module.httpx,
            "Client",
            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
        )
        monkeypatch.setattr(sender_module.time, "sleep", lambda s: None)
        return TelegramSender(self.TOKEN)

    def test_redact_token_masks_only_the_token(self):
        url = f"https://api.telegram.org/bot{self.TOKEN}/sendMessage"
        assert redact_token(url) == "https://api.telegram.org/bot***/sendMessage"
        assert redact_token("/bots/list and /bothering") == "/bots/list and /bothering"

    def test_httpx_request_log_does_not_contain_token(self, monkeypatch, caplog):
        sender = self._sender_with_transport(monkeypatch, lambda req: httpx.Response(200, json={"ok": True}))
        with caplog.at_level(logging.INFO, logger="httpx"):
            sender._post("/sendMessage", json={"chat_id": 1, "text": "hi"})
        messages = [r.getMessage() for r in caplog.records if r.name == "httpx"]
        assert messages, "httpx should still log the request"
        assert all(self.TOKEN not in m for m in messages)
        assert any("/bot***/sendMessage" in m for m in messages)

    def test_http_error_does_not_leak_token(self, monkeypatch):
        sender = self._sender_with_transport(
            monkeypatch, lambda req: httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
        )
        with pytest.raises(TelegramSendError) as exc_info:
            sender._post("/sendMessage", json={"chat_id": 1, "text": "hi"})
        assert self.TOKEN not in str(exc_info.value)
        assert "400" in str(exc_info.value)
        assert exc_info.value.__cause__ is None and exc_info.value.__context__ is None

    def test_transport_error_does_not_leak_token(self, monkeypatch):
        def boom(req):
            raise httpx.ConnectError(f"cannot reach {req.url}", request=req)

        sender = self._sender_with_transport(monkeypatch, boom)
        with pytest.raises(TelegramSendError) as exc_info:
            sender._post("/sendMessage", json={"chat_id": 1, "text": "hi"})
        assert self.TOKEN not in str(exc_info.value)
        assert "ConnectError" in str(exc_info.value)
        # chain suppressed: the original httpx error (with the URL) is not printed
        assert exc_info.value.__suppress_context__

    def test_worker_failure_log_does_not_contain_token(self, monkeypatch, caplog):
        sender = self._sender_with_transport(monkeypatch, lambda req: httpx.Response(401, json={"ok": False}))
        with caplog.at_level(logging.ERROR, logger="telegram_notifier"):
            sender.send_message(-1, "hi")
            sender.flush(timeout=5.0)
        logged = "\n".join(caplog.text.splitlines())
        assert "telegram send failed" in logged
        assert self.TOKEN not in logged

    def test_filter_is_installed_once(self):
        TelegramSender(self.TOKEN)
        TelegramSender(self.TOKEN)
        filters = [f for f in logging.getLogger("httpx").filters if type(f).__name__ == "_RedactBotTokenFilter"]
        assert len(filters) == 1


class TestSecretScrubbing:
    SECRETS = [
        "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "sk-bogus***********test",  # OpenAI echoes masked fragments like this
        "AIzaSyA1234567890abcdefghijklmnopqrstuv",
        "AQ.Ab8RN6J6CQY4t99s42Y2J6sElUCunconFyfsXW4vIys",
        "123456789:AAHsecretsecretsecretsecretsecretsec",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "ndd_abcdefghijklmnopqrstuvwxyz",
    ]

    @pytest.mark.parametrize("secret", SECRETS)
    def test_secret_shapes_are_masked(self, secret):
        out = scrub_secrets(f"Incorrect API key provided: {secret}. See docs")
        assert secret not in out
        assert "[secret]" in out

    def test_bearer_and_query_params_are_masked(self):
        out = scrub_secrets("Authorization: Bearer abcdefghijklmnop https://x.io/v1?key=SECRETVALUE123&alt=json")
        assert "abcdefghijklmnop" not in out and "SECRETVALUE123" not in out
        assert "?key=[secret]&alt=json" in out

    def test_ordinary_text_is_untouched(self):
        text = "menu generation failed user_id=e25d9aa5-a0c5-4c3a-9105-0102701a0615 task-1234 status 402"
        assert scrub_secrets(text) == text

    def test_message_payload_is_scrubbed(self):
        payload = sender_module._message_payload(-1, "key sk-proj-AbCdEfGhIjKlMnOpQrStUv", None, "HTML")
        assert "sk-proj" not in payload["text"]

    def test_document_caption_and_content_are_scrubbed(self):
        data, files = sender_module._document_parts(
            -1, "tb.txt", b"Traceback: AIzaSyA1234567890abcdefghijklmnopqrstuv", "sk-proj-AbCdEfGhIjKlMnOpQrStUv", None
        )
        assert "sk-proj" not in data["caption"]
        assert b"AIza" not in files["document"][1]

    def test_binary_document_is_sent_as_is(self):
        blob = b"\xff\xfe\x00binary"
        _, files = sender_module._document_parts(-1, "x.bin", blob, None, None)
        assert files["document"][1] == blob

    def test_logger_error_with_key_in_exception_never_reaches_telegram(self):
        """End to end through TelegramLogger -> real sender -> captured POST body."""
        tg = make_logger()
        sender = TelegramSender("123:abc", min_interval=0)
        posted = []
        sender._post = lambda method, **kw: posted.append(kw)
        tg._sender = sender
        try:
            raise RuntimeError("Error code: 401 - Incorrect API key provided: sk-proj-AbCdEfGhIjKlMnOpQrStUvWx")
        except RuntimeError as exc:
            tg.error("generation failed", exc=exc, provider_error="key sk-bogus*****test")
        sender.flush(timeout=5.0)
        body = repr(posted)
        assert posted
        assert "sk-proj-AbCd" not in body and "sk-bogus" not in body
        assert "[secret]" in body
