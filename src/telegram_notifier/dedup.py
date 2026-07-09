"""Error deduplication: identical errors within a time window are counted, not sent."""

import threading
import time


def fingerprint(exc: BaseException) -> str:
    """Identify an error by its type and the innermost traceback frame."""
    tb = exc.__traceback__
    last = None
    while tb is not None:
        last = tb
        tb = tb.tb_next
    location = ""
    if last is not None:
        location = f"{last.tb_frame.f_code.co_filename}:{last.tb_lineno}"
    return f"{type(exc).__module__}.{type(exc).__qualname__}@{location}"


class ErrorDeduplicator:
    def __init__(self, window_seconds: float = 300.0):
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._seen: dict[str, tuple[float, int]] = {}

    def check(self, key: str) -> tuple[bool, int]:
        """Return (should_send, suppressed_count).

        First occurrence sends immediately. Repeats within the window are
        suppressed and counted; the first send after the window expires
        reports how many were suppressed.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._seen.get(key)
            if entry is not None:
                started, count = entry
                if now - started < self.window_seconds:
                    self._seen[key] = (started, count + 1)
                    return False, 0
                self._seen[key] = (now, 0)
                return True, count
            self._seen[key] = (now, 0)
            return True, 0