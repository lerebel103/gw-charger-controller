"""Unit tests for MQTTClient command handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ha import MQTTClient
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

        await client._handle_command("ev_charger/switch/plug_and_charge_auto_start/set", "INVALID")

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

        await client._handle_command("ev_charger/switch/plug_and_charge_auto_start/set", "ON")

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

    @pytest.mark.asyncio
    async def test_runtime_advanced_mode_accepts_numeric_payload(self):
        state = AppState(charge_mode="Standby")
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        ev = AsyncMock()
        ev.connected = True
        ev.ensure_connected = AsyncMock()
        ev.disconnect = AsyncMock()
        ev.write_advanced_charging_mode = AsyncMock(return_value=True)
        ev.read_advanced_charging_mode = AsyncMock(return_value=AdvancedChargingMode.PV_BATTERY_HYBRID_CHARGING)

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue, ev_client=ev)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/select/advanced_charging_mode/set", "2")

        ev.write_advanced_charging_mode.assert_awaited_once_with(AdvancedChargingMode.PV_BATTERY_HYBRID_CHARGING)

    @pytest.mark.asyncio
    async def test_runtime_advanced_mode_accepts_fast_charging_case_insensitive(self):
        state = AppState(charge_mode="Standby")
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        ev = AsyncMock()
        ev.connected = True
        ev.ensure_connected = AsyncMock()
        ev.disconnect = AsyncMock()
        ev.write_advanced_charging_mode = AsyncMock(return_value=True)
        ev.read_advanced_charging_mode = AsyncMock(return_value=AdvancedChargingMode.FAST_CHARGING)

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue, ev_client=ev)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/select/advanced_charging_mode/set", "  fast charging  ")

        ev.write_advanced_charging_mode.assert_awaited_once_with(AdvancedChargingMode.FAST_CHARGING)
        ev.read_advanced_charging_mode.assert_awaited_once()
        client._client.publish.assert_any_await(
            "ev_charger/select/advanced_charging_mode/state",
            "Fast charging",
            retain=True,
        )

    @pytest.mark.asyncio
    async def test_runtime_advanced_mode_noop_does_not_connect(self):
        state = AppState(
            charge_mode="Standby",
            ev_advanced_charging_mode_enum=AdvancedChargingMode.PV_CHARGING,
        )
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        ev = AsyncMock()
        ev.connected = True
        ev.ensure_connected = AsyncMock()
        ev.disconnect = AsyncMock()

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue, ev_client=ev)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/select/advanced_charging_mode/set", "PV charging")

        ev.ensure_connected.assert_not_awaited()
        ev.disconnect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runtime_advanced_mode_unknown_label_is_rejected(self):
        state = AppState(charge_mode="Standby")
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        ev = AsyncMock()
        ev.connected = True
        ev.ensure_connected = AsyncMock()
        ev.disconnect = AsyncMock()

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue, ev_client=ev)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/select/advanced_charging_mode/set", "Unknown")

        ev.ensure_connected.assert_not_awaited()
        ev.disconnect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runtime_advanced_mode_unknown_numeric_is_rejected(self):
        state = AppState(charge_mode="Standby")
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        ev = AsyncMock()
        ev.connected = True
        ev.ensure_connected = AsyncMock()
        ev.disconnect = AsyncMock()

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue, ev_client=ev)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/select/advanced_charging_mode/set", "255")

        ev.ensure_connected.assert_not_awaited()
        ev.disconnect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runtime_max_grid_power_draw_allowed_in_standby_with_disconnect(self):
        state = AppState(charge_mode="Standby")
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        ev = AsyncMock()
        ev.connected = True
        ev.ensure_connected = AsyncMock()
        ev.disconnect = AsyncMock()
        ev.write_max_grid_power_draw = AsyncMock(return_value=True)
        ev.read_max_grid_power_draw = AsyncMock(return_value=4200.0)

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue, ev_client=ev)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/number/max_grid_power_draw/set", "4200")

        ev.ensure_connected.assert_awaited_once()
        ev.write_max_grid_power_draw.assert_awaited_once_with(4200.0)
        ev.read_max_grid_power_draw.assert_awaited_once()
        ev.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_runtime_max_grid_power_draw_invalid_range_rejected(self):
        state = AppState(charge_mode="Standby")
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        ev = AsyncMock()
        ev.connected = True
        ev.ensure_connected = AsyncMock()
        ev.disconnect = AsyncMock()

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue, ev_client=ev)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/number/max_grid_power_draw/set", "4100")

        ev.ensure_connected.assert_not_awaited()
        ev.disconnect.assert_not_awaited()


class TestMQTTChargeModeSelect:
    @pytest.mark.asyncio
    async def test_charge_mode_noop_does_not_persist_or_publish(self):
        state = AppState(charge_mode="Eco")
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/select/mode/set", "Eco")

        cfg.schedule_persist.assert_not_called()
        client._client.publish.assert_not_awaited()


class TestMQTTPublishConfigState:
    @pytest.mark.asyncio
    async def test_publish_config_state_handles_zero_valued_runtime_enums(self):
        state = AppState(
            ev_advanced_charging_mode_enum=AdvancedChargingMode.FAST_CHARGING,
            ev_plug_and_charge_auto_start_enum=PlugAndChargeAutoStart.OFF,
            ev_single_phase_switching_enum=SinglePhaseSwitching.DISABLED,
        )
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)
        client._client = AsyncMock()

        await client._publish_config_state()

        client._client.publish.assert_any_await(
            "ev_charger/select/advanced_charging_mode/state",
            "Fast charging",
            retain=True,
        )
        client._client.publish.assert_any_await(
            "ev_charger/switch/plug_and_charge_auto_start/state",
            "OFF",
            retain=True,
        )
        client._client.publish.assert_any_await(
            "ev_charger/switch/single_phase_switching/state",
            "OFF",
            retain=True,
        )


class TestMQTTRunLoop:
    @pytest.mark.asyncio
    async def test_run_loop_clears_publish_fail_on_successful_connect(self):
        state = AppState(mqtt_host="broker", mqtt_port=1883)
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)

        mqtt_client = AsyncMock()
        mqtt_context = AsyncMock()
        mqtt_context.__aenter__.return_value = mqtt_client

        class StopLoopError(Exception):
            pass

        with (
            patch("app.ha.client._throttle") as throttle,
            patch("app.ha.client.aiomqtt.Client", return_value=mqtt_context),
            patch.object(client, "_publish_discovery", new_callable=AsyncMock),
            patch.object(client, "_publish_config_state", new_callable=AsyncMock),
            patch("app.ha.client.asyncio.gather", new_callable=AsyncMock, side_effect=StopLoopError),
            pytest.raises(StopLoopError),
        ):
            await client.run_loop()

        throttle.clear.assert_any_call("mqtt_connect_fail")
        throttle.clear.assert_any_call("mqtt_publish_fail")
        throttle.reset.assert_called_once_with("mqtt_retry")
