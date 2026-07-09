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


def _origin(service_name: str | None, environment: str | None) -> str | None:
    parts = [p for p in (service_name, environment) if p]
    return " · ".join(parts) if parts else None


def build_message(level: str, service_name: str | None, environment: str | None,
                  text: str, fields: dict) -> str:
    """Build the HTML header part of a notification (no traceback).

    Layout:
        <emoji> <b>title</b>
        <i>[service · env]</i>

        key: <code>value</code>   (one field per line; code = tap-to-copy)
    """
    text = text[:TEXT_BUDGET]
    lines = [f"{EMOJI[level]} <b>{html.escape(text)}</b>"]
    origin = _origin(service_name, environment)
    if origin:
        lines.append(f"<i>[{html.escape(origin)}]</i>")
    if fields:
        lines.append("")
        for key, value in fields.items():
            lines.append(f"{html.escape(str(key))}: <code>{html.escape(str(value))}</code>")
    return "\n".join(lines)


def build_plain(level: str, service_name: str | None, environment: str | None,
                text: str, fields: dict) -> str:
    """Plain-text variant, used as a document caption or as a no-HTML fallback."""
    text = text[:TEXT_BUDGET]
    lines = [f"{EMOJI[level]} {text}"]
    origin = _origin(service_name, environment)
    if origin:
        lines.append(f"[{origin}]")
    if fields:
        lines.append("")
        lines.extend(f"{key}: {value}" for key, value in fields.items())
    return "\n".join(lines)


def format_traceback(exc: BaseException, budget: int = TRACEBACK_BUDGET) -> str:
    """Render a traceback, keeping the tail (the cause lives there) within budget."""
    tb = "".join(traceback.format_exception(exc))
    if len(tb) > budget:
        tb = "…\n" + tb[-budget:]
    return tb


def append_traceback(message: str, tb: str) -> str:
    return f"{message}\n<blockquote expandable><pre>{html.escape(tb)}</pre></blockquote>"