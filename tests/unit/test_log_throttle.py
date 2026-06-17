"""Unit tests for the LogThrottle utility."""

from __future__ import annotations

import logging
from unittest.mock import patch

from app.log_throttle import LogThrottle


class TestLogThrottleFirstOccurrence:
    """First call should always log immediately."""

    def test_warning_logs_immediately(self, caplog):
        logger = logging.getLogger("test.throttle.first_warn")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        with caplog.at_level(logging.WARNING, logger="test.throttle.first_warn"):
            throttle.warning("key1", "Something failed: %s", "timeout")

        assert len(caplog.records) == 1
        assert "Something failed: timeout" in caplog.records[0].message

    def test_info_logs_immediately(self, caplog):
        logger = logging.getLogger("test.throttle.first_info")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        with caplog.at_level(logging.INFO, logger="test.throttle.first_info"):
            throttle.info("key1", "Connected to %s", "device")

        assert len(caplog.records) == 1
        assert "Connected to device" in caplog.records[0].message

    def test_error_logs_immediately(self, caplog):
        logger = logging.getLogger("test.throttle.first_error")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        with caplog.at_level(logging.ERROR, logger="test.throttle.first_error"):
            throttle.error("key1", "Critical failure")

        assert len(caplog.records) == 1
        assert "Critical failure" in caplog.records[0].message


class TestLogThrottleSuppression:
    """Subsequent calls within the suppress window should be suppressed."""

    def test_second_call_suppressed(self, caplog):
        logger = logging.getLogger("test.throttle.suppress")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        with caplog.at_level(logging.WARNING, logger="test.throttle.suppress"):
            throttle.warning("key1", "Failure")
            throttle.warning("key1", "Failure")
            throttle.warning("key1", "Failure")

        assert len(caplog.records) == 1

    def test_different_keys_not_suppressed(self, caplog):
        logger = logging.getLogger("test.throttle.diffkeys")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        with caplog.at_level(logging.WARNING, logger="test.throttle.diffkeys"):
            throttle.warning("key_a", "Failure A")
            throttle.warning("key_b", "Failure B")

        assert len(caplog.records) == 2


class TestLogThrottleIntervalResume:
    """After the suppress interval passes, the next call should log with a repeat count."""

    @patch("app.log_throttle.time.monotonic")
    def test_logs_again_after_interval(self, mock_monotonic, caplog):
        logger = logging.getLogger("test.throttle.interval")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        mock_monotonic.return_value = 0.0
        with caplog.at_level(logging.WARNING, logger="test.throttle.interval"):
            throttle.warning("key1", "Failure")  # logs (first)

            mock_monotonic.return_value = 10.0
            throttle.warning("key1", "Failure")  # suppressed (count=1)

            mock_monotonic.return_value = 20.0
            throttle.warning("key1", "Failure")  # suppressed (count=2)

            mock_monotonic.return_value = 65.0
            throttle.warning("key1", "Failure")  # logs with repeat count

        assert len(caplog.records) == 2
        assert "repeated 2 times since last log" in caplog.records[1].message

    @patch("app.log_throttle.time.monotonic")
    def test_repeat_count_resets_after_interval(self, mock_monotonic, caplog):
        logger = logging.getLogger("test.throttle.reset")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        mock_monotonic.return_value = 0.0
        with caplog.at_level(logging.WARNING, logger="test.throttle.reset"):
            throttle.warning("key1", "Failure")  # logs (first)

            mock_monotonic.return_value = 30.0
            throttle.warning("key1", "Failure")  # suppressed (count=1)

            mock_monotonic.return_value = 65.0
            throttle.warning("key1", "Failure")  # logs (repeated 1 times)

            mock_monotonic.return_value = 70.0
            throttle.warning("key1", "Failure")  # suppressed (count=1)

            mock_monotonic.return_value = 130.0
            throttle.warning("key1", "Failure")  # logs (repeated 1 times)

        assert len(caplog.records) == 3
        assert "repeated 1 times" in caplog.records[1].message
        assert "repeated 1 times" in caplog.records[2].message


class TestLogThrottleClear:
    """clear() should log recovery and reset the throttle state."""

    def test_clear_logs_recovery_with_suppressed_count(self, caplog):
        logger = logging.getLogger("test.throttle.clear_count")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        with caplog.at_level(logging.INFO, logger="test.throttle.clear_count"):
            throttle.warning("key1", "Failure")  # first (logged as WARNING)
            throttle.warning("key1", "Failure")  # suppressed
            throttle.warning("key1", "Failure")  # suppressed
            throttle.clear("key1")  # recovery

        # caplog at INFO captures the clear message
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info_records) == 1
        assert "condition cleared" in info_records[0].message
        assert "suppressed 2 repeat(s)" in info_records[0].message

    def test_clear_logs_recovery_even_with_zero_suppressions(self, caplog):
        logger = logging.getLogger("test.throttle.clear_zero")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        with caplog.at_level(logging.INFO, logger="test.throttle.clear_zero"):
            throttle.warning("key1", "Failure")  # first occurrence
            throttle.clear("key1")  # recovery (no suppressed repeats)

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info_records) == 1
        assert "condition cleared" in info_records[0].message

    def test_clear_resets_so_next_call_logs_immediately(self, caplog):
        logger = logging.getLogger("test.throttle.clear_reset")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        with caplog.at_level(logging.WARNING, logger="test.throttle.clear_reset"):
            throttle.warning("key1", "Failure")
            throttle.clear("key1")
            throttle.warning("key1", "Failure again")

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 2
        assert "Failure again" in warning_records[1].message

    def test_clear_inactive_key_is_noop(self, caplog):
        logger = logging.getLogger("test.throttle.clear_noop")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        with caplog.at_level(logging.INFO, logger="test.throttle.clear_noop"):
            throttle.clear("nonexistent_key")

        assert len(caplog.records) == 0

    def test_clear_already_cleared_is_noop(self, caplog):
        logger = logging.getLogger("test.throttle.clear_twice")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        with caplog.at_level(logging.INFO, logger="test.throttle.clear_twice"):
            throttle.warning("key1", "Failure")
            throttle.clear("key1")
            caplog.clear()
            throttle.clear("key1")  # second clear is noop

        assert len(caplog.records) == 0


class TestLogThrottleReset:
    """reset() should clear throttle state without logging recovery."""

    def test_reset_is_silent_and_next_call_logs_immediately(self, caplog):
        logger = logging.getLogger("test.throttle.reset_silent")
        throttle = LogThrottle(logger, suppress_seconds=60.0)

        with caplog.at_level(logging.INFO, logger="test.throttle.reset_silent"):
            throttle.info("key1", "Connected")
            throttle.reset("key1")
            throttle.info("key1", "Connected again")

        info_messages = [record.message for record in caplog.records if record.levelno == logging.INFO]
        assert info_messages == ["Connected", "Connected again"]
