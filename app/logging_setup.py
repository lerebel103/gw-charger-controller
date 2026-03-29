"""Structured logging configuration for the EV charger integration."""

from __future__ import annotations

import logging
import os
import sys


def setup_logging() -> None:
    """Configure the root logger with structured formatting.

    Format includes timestamp (ISO), log level, and component name (logger name).
    Reads the ``LOG_LEVEL`` environment variable to override the default INFO level.
    """
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))
    root.addHandler(handler)
