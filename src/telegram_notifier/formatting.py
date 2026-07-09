"""Message building: HTML escaping, context fields, expandable traceback blocks."""

import html
import traceback

TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024

# Raw traceback budget; keeps the final HTML message comfortably under the limit
# in the common case. Oversized results fall back to a document upload.
TRACEBACK_BUDGET = 3000

# User-supplied summary text is capped so the header part of a message can
# never overflow the Telegram limit on its own.
TEXT_BUDGET = 1000

EMOJI = {
    "info": "ℹ️",
    "warning": "⚠️",
    "error": "❌",
    "user_action": "\U0001f464",
}


def format_fields(fields: dict) -> str:
    return " | ".join(f"{key}={value}" for key, value in fields.items())


def build_message(level: str, service_name: str | None, text: str, fields: dict) -> str:
    """Build the HTML header part of a notification (no traceback)."""
    text = text[:TEXT_BUDGET]
    parts = [EMOJI[level], " "]
    if service_name:
        parts.append(f"<b>[{html.escape(service_name)}]</b> ")
    parts.append(html.escape(text))
    if fields:
        parts.append("\n<code>" + html.escape(format_fields(fields)) + "</code>")
    return "".join(parts)


def build_plain(level: str, service_name: str | None, text: str, fields: dict) -> str:
    """Plain-text variant, used as a document caption or as a no-HTML fallback."""
    text = text[:TEXT_BUDGET]
    prefix = f"[{service_name}] " if service_name else ""
    message = f"{EMOJI[level]} {prefix}{text}"
    if fields:
        message += "\n" + format_fields(fields)
    return message


def format_traceback(exc: BaseException, budget: int = TRACEBACK_BUDGET) -> str:
    """Render a traceback, keeping the tail (the cause lives there) within budget."""
    tb = "".join(traceback.format_exception(exc))
    if len(tb) > budget:
        tb = "…\n" + tb[-budget:]
    return tb


def append_traceback(message: str, tb: str) -> str:
    return f"{message}\n<blockquote expandable><pre>{html.escape(tb)}</pre></blockquote>"