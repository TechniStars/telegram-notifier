# telegram-notifier

Lightweight Telegram notifications for services. One supergroup with topics
(e.g. Info / Warnings / Errors / UserActions), one bot per service, messages
routed to topics by level.

- **Fire-and-forget** — a failed send never raises into your code; sync calls
  dispatch from a daemon thread, async calls swallow errors after awaiting.
- **Error dedup** — identical errors (same exception type + innermost frame)
  within a time window are counted, not re-sent: `failed (×47 suppressed in last 300s)`.
- **Readable tracebacks** — collapsed `<blockquote expandable>` block; oversized
  tracebacks are uploaded as a `traceback.txt` document instead.
- **Safe by default** — without `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` the
  logger is disabled and every call is a no-op (dev, tests, CI).

## How routing works

Telegram topic **names are irrelevant** to this package. Routing is explicit:

1. The caller picks the level by picking the method: `tg.info(...)` routes to
   the `"info"` topic, `tg.error(...)` to `"error"`, etc. There is no per-call
   topic argument.
2. The `topics` dict passed to the constructor translates level →
   `message_thread_id`. That id is a plain int: Telegram assigns it when a
   topic is created (it is the `message_id` of the "topic created" service
   message), and every message posted in that topic carries it. Ids are
   per-group — a new group means new ids.
3. A level missing from the dict posts to the group's General topic.

Tracebacks: only `error()` handles them — pass the exception as `exc=`:
`tg.error("summary", exc=e)`. Without `exc` it's just a text message. Every
method accepts extra `**fields` kwargs, appended as a `key=value | key=value`
context line.

## End-to-end setup

### 1. Create the bot

Talk to **@BotFather** → `/newbot` → pick a name and username. Save the token
(`123456:AAF...`). Recommended layout: one bot per service — several bots can
post into the same group and stay distinguishable.

### 2. Create the group with topics

1. Create a new group (e.g. `MYAPP_LOGS`).
2. Group settings → enable **Topics**. The group becomes a forum-style supergroup.
3. Create one topic per level, e.g. *Info*, *Warnings*, *Errors*, *UserActions*.
   Names are for humans only — the package uses the numeric ids.

### 3. Add the bot

Add the bot as a member and promote it to **admin** (simplest way for it to see
group messages; alternatively disable its privacy mode via @BotFather
`/setprivacy`). Needed for the discovery step below.

### 4. Discover chat_id and topic ids

The package installs a console command for this. Send one throwaway message
**in each topic** (the bot only sees messages sent after it joined), then:

```
pip install git+ssh://git@github.com/<you>/telegram-notifier.git
TELEGRAM_BOT_TOKEN=<token> telegram-notifier-discover
```

Output:

```
chat_id=-1001234567890  thread_id=None  topic=General
chat_id=-1001234567890  thread_id=4     topic=Info
chat_id=-1001234567890  thread_id=6     topic=Warnings
chat_id=-1001234567890  thread_id=8     topic=Errors
chat_id=-1001234567890  thread_id=10    topic=UserActions
```

If it prints `No updates`: check the bot is admin, re-send a message in each
topic, run again. Run this once per group and copy the ids into your project
(step 6) — they never change.

### 5. Smoke test (optional)

From a checkout of this repo, edit the `topics` dict in `scripts/smoke_test.py`
to match your ids, then:

```
TELEGRAM_BOT_TOKEN=<token> TELEGRAM_CHAT_ID=<chat_id> python scripts/smoke_test.py
```

Sends one message per topic, including an error with a collapsed traceback and
a duplicate error that dedup should suppress (only one error message appears).

### 6. Integrate into a project

Add the dependency (`requirements.txt` / `pyproject.toml`):

```
telegram-notifier @ git+ssh://git@github.com/<you>/telegram-notifier.git
```

Set env vars in the deployment — and **leave them unset** in dev/tests, the
logger then no-ops:

```
TELEGRAM_BOT_TOKEN=123456:AAF...
TELEGRAM_CHAT_ID=-1001234567890
```

Create one shared instance per service, e.g. `app/notify.py` — this is where
the level → thread_id mapping lives (hardcoded is fine, the ids are constants,
not secrets):

```python
from telegram_notifier import TelegramLogger

tg = TelegramLogger(
    # bot_token / chat_id default to TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars
    topics={"info": 4, "warning": 6, "error": 8, "user_action": 10},
    service_name="service1",
    environment="prod",  # defaults to TELEGRAM_ENVIRONMENT env var; renders as [service1][prod]
)
```

Call it from anywhere:

```python
from app.notify import tg

tg.info("worker started", queue="agentic")
tg.warning("retry 3/5", job_id="xyz")
tg.user_action("user_registered", user_id=123)

try:
    ...
except Exception as e:
    tg.error("pipeline failed", exc=e, user_id=123)
    raise
```

Async variants: `await tg.ainfo(...)`, `awarning`, `aerror`, `auser_action`.

### 7. Keep the group useful

Notify explicit business events and critical errors only — do not wire this up
as a handler for your entire `logging` output, or the group turns into noise.

## Options

| Param | Default | Meaning |
|---|---|---|
| `bot_token` | env `TELEGRAM_BOT_TOKEN` | bot API token |
| `chat_id` | env `TELEGRAM_CHAT_ID` | target group id (e.g. `-100…`) |
| `topics` | `{}` | level → `message_thread_id`; missing level posts to General |
| `service_name` | `None` | bold prefix `[name]` in every message |
| `environment` | env `TELEGRAM_ENVIRONMENT` | extra prefix `[prod]` / `[dev]` / `[local]` after the service name |
| `dedup_window_seconds` | `300` | error suppression window |
| `blocking` | `False` | sync methods send inline instead of daemon thread |
| `timeout` | `10` | HTTP timeout seconds |

## Tests

```
pip install -e .[dev]
pytest
```