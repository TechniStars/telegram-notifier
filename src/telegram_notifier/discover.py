"""Console tool: print chat_id and topic thread_ids seen by the bot.

Prerequisites:
    - the bot is an admin of the group (or has privacy mode disabled),
    - at least one message was sent in each topic after the bot joined.

Usage:
    TELEGRAM_BOT_TOKEN=... telegram-notifier-discover
"""

import os
import sys

import httpx


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Set TELEGRAM_BOT_TOKEN")

    resp = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30)
    resp.raise_for_status()
    updates = resp.json()["result"]

    if not updates:
        sys.exit("No updates. Send a message in each topic first (and check the bot is admin).")

    seen: dict[tuple[int, int | None], str] = {}
    for update in updates:
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue
        chat = msg["chat"]
        thread_id = msg.get("message_thread_id")
        name = "General"
        if "forum_topic_created" in msg:
            name = msg["forum_topic_created"]["name"]
        elif "reply_to_message" in msg and "forum_topic_created" in msg["reply_to_message"]:
            name = msg["reply_to_message"]["forum_topic_created"]["name"]
        key = (chat["id"], thread_id)
        if key not in seen or name != "General":
            seen[key] = name

    for (chat_id, thread_id), name in sorted(seen.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        print(f"chat_id={chat_id}  thread_id={thread_id}  topic={name}")


if __name__ == "__main__":
    main()