"""Log throttle utility to suppress noisy recurring log messages.

Logs the first occurrence of a message immediately, then suppresses repeats
for a configurable interval. When the message recurs after suppression, it
logs the message again along with a count of how many times it was suppressed.
When the caller explicitly calls ``clear(key)`` to signal recovery, a summary
is logged indicating the condition has resolved.
"""

from __future__ import annotations

import logging
import time


class LogThrottle:
    """Throttle repeated log messages to first + periodic summary.

    Usage:
        _throttle = LogThrottle(logger, suppress_seconds=60)

        # In a loop:
        _throttle.warning("victron_read_fail", "Victron GX read failed: %s", exc)

    The first call logs immediately. Subsequent calls within `suppress_seconds`
    are counted but not emitted. After the interval expires, the next occurrence
    logs along with the suppressed count.
    """

    def __init__(self, logger: logging.Logger, suppress_seconds: float = 60.0) -> None:
        self._logger = logger
        self._suppress_seconds = suppress_seconds
        self._entries: dict[str, _ThrottleEntry] = {}

    def _get_entry(self, key: str) -> _ThrottleEntry:
        if key not in self._entries:
            self._entries[key] = _ThrottleEntry()
        return self._entries[key]

    def _should_log(self, key: str) -> tuple[bool, int]:
        """Return (should_log, suppressed_count) for the given key."""
        entry = self._get_entry(key)
        now = time.monotonic()

        if entry.first_logged_at is None:
            # First occurrence ever
            entry.first_logged_at = now
            entry.last_logged_at = now
            entry.suppressed_count = 0
            entry.active = True
            return True, 0

        elapsed = now - entry.last_logged_at
        if elapsed >= self._suppress_seconds:
            # Interval passed, log again with count
            suppressed = entry.suppressed_count
            entry.last_logged_at = now
            entry.suppressed_count = 0
            return True, suppressed

        # Suppress
        entry.suppressed_count += 1
        return False, 0

    def clear(self, key: str) -> None:
        """Mark a condition as resolved. Logs a recovery message if it was active."""
        entry = self._entries.get(key)
        if entry is not None and entry.active:
            total = entry.suppressed_count
            if total > 0:
                self._logger.info(
                    "%s: condition cleared (suppressed %d repeat(s) during event)",
                    key,
                    total,
                )
            else:
                self._logger.info("%s: condition cleared", key)
            entry.active = False
            entry.first_logged_at = None
            entry.last_logged_at = None
            entry.suppressed_count = 0

    def warning(self, key: str, msg: str, *args: object) -> None:
        """Throttled warning log."""
        should_log, suppressed = self._should_log(key)
        if should_log:
            if suppressed > 0:
                self._logger.warning(msg + " (repeated %d times since last log)", *args, suppressed)
            else:
                self._logger.warning(msg, *args)

    def info(self, key: str, msg: str, *args: object) -> None:
        """Throttled info log."""
        should_log, suppressed = self._should_log(key)
        if should_log:
            if suppressed > 0:
                self._logger.info(msg + " (repeated %d times since last log)", *args, suppressed)
            else:
                self._logger.info(msg, *args)

    def error(self, key: str, msg: str, *args: object) -> None:
        """Throttled error log."""
        should_log, suppressed = self._should_log(key)
        if should_log:
            if suppressed > 0:
                self._logger.error(msg + " (repeated %d times since last log)", *args, suppressed)
            else:
                self._logger.error(msg, *args)


class _ThrottleEntry:
    """Internal state for a single throttled message key."""

    __slots__ = ("first_logged_at", "last_logged_at", "suppressed_count", "active")

    def __init__(self) -> None:
        self.first_logged_at: float | None = None
        self.last_logged_at: float | None = None
        self.suppressed_count: int = 0
        self.active: bool = False
