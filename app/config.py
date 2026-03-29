"""Configuration manager for the EV charger integration."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from pathlib import Path

import yaml

from app.state import PERSISTED_FIELDS, AppState

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = ("mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password")


class ConfigError(Exception):
    """Raised when the configuration file is missing, unparseable, or incomplete."""


class ConfigManager:
    """Loads config on startup and debounce-persists updates back to YAML."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._dirty = False
        self._last_dirty: float = 0.0
        self._state: AppState | None = None

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def load(self) -> AppState:
        """Read YAML config, validate required fields, return a populated AppState."""
        try:
            raw = Path(self._path).read_text()
        except FileNotFoundError as exc:
            raise ConfigError(f"Config file not found: {self._path}") from exc
        except OSError as exc:
            raise ConfigError(f"Cannot read config file: {self._path}: {exc}") from exc

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in {self._path}: {exc}") from exc

        if not isinstance(data, dict):
            raise ConfigError(f"Invalid YAML in {self._path}: expected a mapping")

        for field in REQUIRED_FIELDS:
            if field not in data:
                raise ConfigError(f"Missing required config field: {field}")

        state = AppState()
        for key, value in data.items():
            if hasattr(state, key):
                setattr(state, key, value)

        self._state = state
        return state

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def schedule_persist(self, state: AppState) -> None:
        """Mark state as dirty so flush_loop writes it within 5 seconds."""
        self._state = state
        self._dirty = True
        self._last_dirty = time.monotonic()

    async def flush_loop(self) -> None:
        """Async task: check the dirty flag and write config to YAML within 5 s."""
        while True:
            await asyncio.sleep(1)
            if not self._dirty:
                continue
            elapsed = time.monotonic() - self._last_dirty
            if elapsed < 5:
                # Wait until 5 s have passed since the last dirty mark
                await asyncio.sleep(5 - elapsed)
            # Re-check: another schedule_persist may have arrived
            if not self._dirty:
                continue
            self._dirty = False
            await self._write()

    async def _write(self) -> None:
        """Serialise PERSISTED_FIELDS from the current state to YAML on disk."""
        if self._state is None:
            return
        full = asdict(self._state)
        data = {k: v for k, v in full.items() if k in PERSISTED_FIELDS}
        try:
            await asyncio.to_thread(self._write_sync, data)
        except OSError:
            logger.error("Failed to write config file: %s", self._path)

    def _write_sync(self, data: dict) -> None:
        Path(self._path).write_text(yaml.dump(data, default_flow_style=False))
