import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_notifier import TelegramLogger
from telegram_notifier.dedup import ErrorDeduplicator, fingerprint
from telegram_notifier.formatting import (
    TELEGRAM_MESSAGE_LIMIT,
    append_traceback,
    build_message,
    format_traceback,
)


def make_logger(**kwargs) -> TelegramLogger:
    tg = TelegramLogger(
        bot_token="123:abc",
        chat_id=-100123,
        topics={"info": 2, "error": 7},
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
        tg._sender = MagicMock(asend_message=AsyncMock(), asend_document=AsyncMock())

        async def run():
            await tg.ainfo("hello", user_id=1)
            await tg.aerror("bad", exc=raise_and_catch())

        asyncio.run(run())
        assert tg._sender.asend_message.await_count == 2