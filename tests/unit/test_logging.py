"""Unit tests for the structured logging setup."""

from __future__ import annotations

import logging
import os

from app.logging_setup import setup_logging


class TestSetupLogging:
    """Tests for setup_logging()."""

    def test_root_logger_has_handler(self):
        root = logging.getLogger()
        root.handlers.clear()
        setup_logging()
        assert len(root.handlers) == 1
        root.handlers.clear()

    def test_default_level_is_info(self):
        root = logging.getLogger()
        root.handlers.clear()
        env = os.environ.pop("LOG_LEVEL", None)
        try:
            setup_logging()
            assert root.level == logging.INFO
        finally:
            if env is not None:
                os.environ["LOG_LEVEL"] = env
            root.handlers.clear()

    def test_log_level_env_override(self, monkeypatch):
        root = logging.getLogger()
        root.handlers.clear()
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        setup_logging()
        assert root.level == logging.DEBUG
        root.handlers.clear()

    def test_format_contains_required_fields(self, capsys):
        root = logging.getLogger()
        root.handlers.clear()
        setup_logging()
        logger = logging.getLogger("test.component")
        logger.info("hello world")
        captured = capsys.readouterr()
        assert "[INFO]" in captured.out
        assert "test.component" in captured.out
        assert "hello world" in captured.out
        # Timestamp check: ISO format starts with a digit (year)
        assert captured.out[0].isdigit()
        root.handlers.clear()

    def test_invalid_log_level_falls_back_to_info(self, monkeypatch):
        root = logging.getLogger()
        root.handlers.clear()
        monkeypatch.setenv("LOG_LEVEL", "NOTAVALIDLEVEL")
        setup_logging()
        assert root.level == logging.INFO
        root.handlers.clear()
