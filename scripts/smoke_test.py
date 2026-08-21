"""Send one test message to every topic, including an error with a traceback.

Usage:
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python scripts/smoke_test.py
"""

from telegram_notifier import TelegramLogger

tg = TelegramLogger(
    topics={"info": 4, "warning": 6, "error": 8, "user_action": 10},
    service_name="smoke-test",
    blocking=True,
)

if not tg.enabled:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

tg.info("worker started", queue="agentic", replicas=2)
tg.warning("retry 3/5", job_id="abc-123")
tg.other("user_action", "user_registered", user_id=42, email="test@example.com")


def inner():
    raise ValueError("invalid profile state: missing attributes for user <42>")


def outer():
    inner()


try:
    outer()
except ValueError as e:
    tg.error("pipeline failed", exc=e, user_id=42, pipeline="dietary")

# duplicate within dedup window — should be suppressed (no second error message)
try:
    outer()
except ValueError as e:
    tg.error("pipeline failed", exc=e, user_id=42, pipeline="dietary")

print("sent: info, other(user_action), error (+1 duplicate suppressed)")
