"""Unit tests for MQTTClient command handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mqtt_client import MQTTClient
from app.state import AdvancedChargingMode, AppState, PlugAndChargeAutoStart, SinglePhaseSwitching


class TestMQTTRuntimeEVSelects:
    @pytest.mark.asyncio
    async def test_runtime_select_allowed_in_standby_with_disconnect(self):
        state = AppState(charge_mode="Standby")
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        ev = AsyncMock()
        ev.connected = True
        ev.ensure_connected = AsyncMock()
        ev.disconnect = AsyncMock()
        ev.write_advanced_charging_mode = AsyncMock(return_value=True)
        ev.read_advanced_charging_mode = AsyncMock(return_value=AdvancedChargingMode.PV_CHARGING)

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue, ev_client=ev)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/select/advanced_charging_mode/set", "PV charging")

        ev.ensure_connected.assert_awaited_once()
        ev.write_advanced_charging_mode.assert_awaited_once_with(AdvancedChargingMode.PV_CHARGING)
        ev.read_advanced_charging_mode.assert_awaited_once()
        ev.disconnect.assert_awaited_once()
        client._client.publish.assert_awaited()
        cfg.schedule_persist.assert_not_called()

    @pytest.mark.asyncio
    async def test_runtime_select_invalid_option_is_rejected(self):
        state = AppState(charge_mode="Standby")
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        ev = AsyncMock()
        ev.connected = True
        ev.ensure_connected = AsyncMock()
        ev.disconnect = AsyncMock()

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue, ev_client=ev)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/select/plug_and_charge_auto_start/set", "INVALID")

        ev.ensure_connected.assert_not_awaited()
        ev.disconnect.assert_not_awaited()
        cfg.schedule_persist.assert_not_called()

    @pytest.mark.asyncio
    async def test_runtime_select_non_standby_does_not_disconnect(self):
        state = AppState(charge_mode="Eco")
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        ev = AsyncMock()
        ev.connected = True
        ev.ensure_connected = AsyncMock()
        ev.disconnect = AsyncMock()
        ev.write_plug_and_charge_auto_start = AsyncMock(return_value=True)
        ev.read_plug_and_charge_auto_start = AsyncMock(return_value=PlugAndChargeAutoStart.ON)

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue, ev_client=ev)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/select/plug_and_charge_auto_start/set", "On")

        ev.ensure_connected.assert_awaited_once()
        ev.write_plug_and_charge_auto_start.assert_awaited_once_with(PlugAndChargeAutoStart.ON)
        ev.read_plug_and_charge_auto_start.assert_awaited_once()
        ev.disconnect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runtime_switch_allowed_in_standby_with_disconnect(self):
        state = AppState(charge_mode="Standby")
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        ev = AsyncMock()
        ev.connected = True
        ev.ensure_connected = AsyncMock()
        ev.disconnect = AsyncMock()
        ev.write_single_phase_switching = AsyncMock(return_value=True)
        ev.read_single_phase_switching = AsyncMock(return_value=SinglePhaseSwitching.ENABLED)

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue, ev_client=ev)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/switch/single_phase_switching/set", "ON")

        ev.ensure_connected.assert_awaited_once()
        ev.write_single_phase_switching.assert_awaited_once_with(SinglePhaseSwitching.ENABLED)
        ev.read_single_phase_switching.assert_awaited_once()
        ev.disconnect.assert_awaited_once()
