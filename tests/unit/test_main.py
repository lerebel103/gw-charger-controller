"""Unit tests for app.main."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

import pytest

from app.main import _async_main, _supervised, main


@pytest.mark.asyncio
async def test_supervised_restarts_on_exception():
    """_supervised restarts the coroutine after an unhandled exception."""
    call_count = 0

    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RuntimeError("boom")
        # On third call, just cancel ourselves to stop the loop
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _supervised(flaky, "test", _restart_delay=0)

    assert call_count == 3


@pytest.mark.asyncio
async def test_supervised_propagates_cancellation():
    """_supervised re-raises CancelledError immediately."""

    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _supervised(cancelled, "test")


@pytest.mark.asyncio
async def test_async_main_exits_on_config_error(tmp_path):
    """_async_main logs ERROR and exits when config is invalid."""
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text("not_a_valid: true\n")

    with pytest.raises(SystemExit) as exc_info:
        await _async_main(str(bad_config))

    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_async_main_exits_on_missing_config(tmp_path):
    """_async_main exits when config file does not exist."""
    with pytest.raises(SystemExit) as exc_info:
        await _async_main(str(tmp_path / "nonexistent.yaml"))

    assert exc_info.value.code == 1


def test_main_parses_config_flag(monkeypatch):
    """main() passes --config to _async_main."""
    monkeypatch.setattr(sys, "argv", ["app.main", "--config", "custom.yaml"])

    called_with = {}

    async def fake_async_main(config_path: str) -> None:
        called_with["path"] = config_path

    with (
        patch("app.main.setup_logging"),
        patch("app.main._async_main", side_effect=fake_async_main),
        patch("asyncio.run", side_effect=lambda coro: asyncio.get_event_loop().run_until_complete(coro)),
    ):
        main()

    assert called_with["path"] == "custom.yaml"


def test_main_default_config(monkeypatch):
    """main() defaults to config.yaml."""
    monkeypatch.setattr(sys, "argv", ["app.main"])

    called_with = {}

    async def fake_async_main(config_path: str) -> None:
        called_with["path"] = config_path

    with (
        patch("app.main.setup_logging"),
        patch("app.main._async_main", side_effect=fake_async_main),
        patch("asyncio.run", side_effect=lambda coro: asyncio.get_event_loop().run_until_complete(coro)),
    ):
        main()

    assert called_with["path"] == "config.yaml"
