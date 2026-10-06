"""Unit tests for MQTTClient command handling."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ha import MQTTClient
from app.ha.constants import (
    AVAILABILITY_TOPIC,
    PAYLOAD_AVAILABLE,
    PAYLOAD_NOT_AVAILABLE,
    SENSOR_EXPIRE_AFTER,
)
from app.state import (
    AdvancedChargingMode,
    AppState,
    PlugAndChargeAutoStart,
    SinglePhaseSwitching,
    StateSnapshot,
)


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
        mqtt_context.__aexit__.return_value = False

        class StopLoopError(Exception):
            pass

        async def _gather_and_stop(*aws):
            for aw in aws:
                await aw
            raise StopLoopError

        with (
            patch("app.ha.client._throttle") as throttle,
            patch("app.ha.client.aiomqtt.Client", return_value=mqtt_context),
            patch.object(client, "_publish_discovery", new_callable=AsyncMock),
            patch.object(client, "_publish_config_state", new_callable=AsyncMock),
            patch.object(client, "_drain_queue", new_callable=AsyncMock),
            patch.object(client, "_process_messages", new_callable=AsyncMock),
            patch("app.ha.client.asyncio.gather", new=AsyncMock(side_effect=_gather_and_stop)),
            pytest.raises(StopLoopError),
        ):
            await client.run_loop()

        throttle.clear.assert_any_call("mqtt_connect_fail")
        throttle.clear.assert_any_call("mqtt_publish_fail")
        throttle.reset.assert_called_once_with("mqtt_retry")


class TestMQTTSwitchVtype:
    """Tests for the generic persisted config switch handler (vtype == 'switch')."""

    @pytest.mark.asyncio
    async def test_switch_on_updates_state_and_persists(self):
        state = AppState(eco_day_min_charge_enabled=False)
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/switch/eco_day_min_charge/set", "ON")

        assert state.eco_day_min_charge_enabled is True
        cfg.schedule_persist.assert_called_once_with(state)
        client._client.publish.assert_awaited_once_with("ev_charger/switch/eco_day_min_charge/state", "ON", retain=True)

    @pytest.mark.asyncio
    async def test_switch_off_updates_state_and_persists(self):
        state = AppState(eco_day_min_charge_enabled=True)
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/switch/eco_day_min_charge/set", "OFF")

        assert state.eco_day_min_charge_enabled is False
        cfg.schedule_persist.assert_called_once_with(state)
        client._client.publish.assert_awaited_once_with(
            "ev_charger/switch/eco_day_min_charge/state", "OFF", retain=True
        )

    @pytest.mark.asyncio
    async def test_switch_invalid_payload_rejected_and_echoed(self):
        state = AppState(eco_day_min_charge_enabled=True)
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/switch/eco_day_min_charge/set", "INVALID")

        assert state.eco_day_min_charge_enabled is True
        cfg.schedule_persist.assert_not_called()
        # Should echo current value back
        client._client.publish.assert_awaited_once_with("ev_charger/switch/eco_day_min_charge/state", "ON", retain=True)

    @pytest.mark.asyncio
    async def test_switch_noop_does_not_persist(self):
        state = AppState(eco_day_min_charge_enabled=True)
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/switch/eco_day_min_charge/set", "ON")

        cfg.schedule_persist.assert_not_called()
        client._client.publish.assert_not_awaited()


def _published(client: AsyncMock) -> dict[str, object]:
    """Return a {topic: payload} map from a mocked client's publish calls."""
    result: dict[str, object] = {}
    for call in client.publish.await_args_list:
        topic = call.args[0] if call.args else call.kwargs.get("topic")
        payload = call.args[1] if len(call.args) > 1 else call.kwargs.get("payload")
        result[topic] = payload
    return result


# Numeric/enum state topics that must never receive the string "unavailable".
_NUMERIC_STATE_TOPICS = [
    "ev_charger/sensor/power/state",
    "ev_charger/sensor/session_energy/state",
    "ev_charger/sensor/total_energy/state",
    "ev_charger/sensor/voltage_l1/state",
    "ev_charger/sensor/voltage_l2/state",
    "ev_charger/sensor/voltage_l3/state",
    "ev_charger/sensor/current_l1/state",
    "ev_charger/sensor/current_l2/state",
    "ev_charger/sensor/current_l3/state",
    "ev_charger/sensor/setpoint/state",
    "ev_charger/sensor/l1_voltage_drop_perc/state",
    "ev_charger/sensor/l2_voltage_drop_perc/state",
    "ev_charger/sensor/l3_voltage_drop_perc/state",
    "ev_charger/sensor/l1_breaker_headroom/state",
    "ev_charger/sensor/l2_breaker_headroom/state",
    "ev_charger/sensor/l3_breaker_headroom/state",
    "ev_charger/sensor/grid_current_l1/state",
    "ev_charger/sensor/grid_current_l2/state",
    "ev_charger/sensor/grid_current_l3/state",
    "ev_charger/sensor/completion_time/state",
    "ev_charger/sensor/ev_soc/state",
    "ev_charger/sensor/status/state",
]


class TestMQTTDiscovery:
    @pytest.mark.asyncio
    async def test_discovery_payloads_include_availability_and_expire_after(self):
        state = AppState()
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)
        client._client = AsyncMock()

        await client._publish_discovery()

        config_calls = [
            call
            for call in client._client.publish.await_args_list
            if str(call.args[0]).startswith("homeassistant/") and call.args[1]
        ]
        assert config_calls, "expected discovery config payloads to be published"

        for call in config_calls:
            component = str(call.args[0]).split("/")[1]
            payload = json.loads(call.args[1])
            assert payload["availability_topic"] == AVAILABILITY_TOPIC
            assert payload["payload_available"] == PAYLOAD_AVAILABLE
            assert payload["payload_not_available"] == PAYLOAD_NOT_AVAILABLE
            if component == "sensor":
                assert payload["expire_after"] == SENSOR_EXPIRE_AFTER
            else:
                assert "expire_after" not in payload


class TestMQTTPublishStateTransientErrors:
    """A transient-error window must never put a non-numeric string on a numeric topic."""

    @pytest.mark.asyncio
    async def test_all_none_snapshot_publishes_no_unavailable_strings(self):
        state = AppState()
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)
        client._client = AsyncMock()

        # Every numeric/enum/comm field is None (startup / stale / comms failure).
        snapshot = StateSnapshot()
        await client._publish_state(snapshot)

        published = _published(client._client)

        # No topic ever receives the literal "unavailable".
        assert "unavailable" not in published.values()

        # None-valued numeric/enum topics are not published at all.
        for topic in _NUMERIC_STATE_TOPICS:
            assert topic not in published, f"{topic} should be skipped when value is None"

        # Switch/select echoes are skipped when unknown.
        assert "ev_charger/select/advanced_charging_mode/state" not in published
        assert "ev_charger/switch/single_phase_switching/state" not in published
        assert "ev_charger/switch/plug_and_charge_auto_start/state" not in published

        # Binary comm sensors are skipped when None (never "unavailable").
        for slug in (
            "comm_wifi_router",
            "comm_iot_cloud",
            "comm_inverter",
            "comm_mid_meter",
            "comm_gw_meter",
            "comm_ems",
        ):
            assert f"ev_charger/binary_sensor/{slug}/state" not in published

        # Always-safe fields still publish.
        assert published["ev_charger/binary_sensor/connected/state"] == "OFF"
        assert "ev_charger/sensor/uptime/state" in published

    @pytest.mark.asyncio
    async def test_happy_path_snapshot_publishes_numeric_values(self):
        state = AppState()
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)
        client._client = AsyncMock()

        snapshot = StateSnapshot(
            ev_active_power_w=1500.0,
            ev_soc_pct=42.0,
            l1_voltage_drop_pct=1.234,
            l2_voltage_drop_pct=2.0,
            l3_voltage_drop_pct=3.0,
            l1_breaker_headroom_pct=50.0,
            l2_breaker_headroom_pct=60.0,
            l3_breaker_headroom_pct=70.0,
            ev_charger_status_display="Charging",
            ev_comm_wifi_router_connected=True,
            ev_comm_iot_cloud_connected=False,
            ev_single_phase_switching_display="Enabled",
            ev_plug_and_charge_auto_start_display="Off",
        )
        await client._publish_state(snapshot)

        published = _published(client._client)
        assert published["ev_charger/sensor/power/state"] == "1500.0"
        assert published["ev_charger/sensor/ev_soc/state"] == "42.0"
        assert published["ev_charger/sensor/l1_voltage_drop_perc/state"] == "1.23"
        assert published["ev_charger/sensor/l1_breaker_headroom/state"] == "50.0"
        assert published["ev_charger/sensor/status/state"] == "Charging"
        assert published["ev_charger/binary_sensor/comm_wifi_router/state"] == "ON"
        assert published["ev_charger/binary_sensor/comm_iot_cloud/state"] == "OFF"
        assert published["ev_charger/switch/single_phase_switching/state"] == "ON"
        assert published["ev_charger/switch/plug_and_charge_auto_start/state"] == "OFF"
        assert "unavailable" not in published.values()


class TestMQTTPublishStateEVUnhealthy:
    """During an EV comms outage the snapshot masks EV fields to None, so their
    state topics must not be republished (letting expire_after elapse) while
    non-EV topics keep publishing valid values."""

    @pytest.mark.asyncio
    async def test_masked_ev_snapshot_skips_ev_topics_but_keeps_non_ev(self):
        state = AppState()
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)
        client._client = AsyncMock()

        # Snapshot AFTER masking: every EV-derived field is None (as build_snapshot
        # produces when ev_comm_healthy is False), while non-EV fields are valid.
        snapshot = StateSnapshot(
            ev_connected=None,
            l1_breaker_headroom_pct=50.0,
            l2_breaker_headroom_pct=60.0,
            l3_breaker_headroom_pct=70.0,
            victron_l1_current_a=5.0,
            uptime_s=10,
        )
        await client._publish_state(snapshot)

        published = _published(client._client)

        # No topic ever receives the literal "unavailable".
        assert "unavailable" not in published.values()

        # EV numeric/enum topics are skipped (expire_after can elapse).
        for topic in _NUMERIC_STATE_TOPICS:
            if topic in (
                "ev_charger/sensor/l1_breaker_headroom/state",
                "ev_charger/sensor/l2_breaker_headroom/state",
                "ev_charger/sensor/l3_breaker_headroom/state",
                "ev_charger/sensor/grid_current_l1/state",
            ):
                continue
            assert topic not in published, f"{topic} should be skipped during EV outage"

        # Masked connected binary_sensor is skipped (now via _pub_if).
        assert "ev_charger/binary_sensor/connected/state" not in published
        for slug in (
            "comm_wifi_router",
            "comm_iot_cloud",
            "comm_inverter",
            "comm_mid_meter",
            "comm_gw_meter",
            "comm_ems",
        ):
            assert f"ev_charger/binary_sensor/{slug}/state" not in published

        # Non-EV topics still publish their valid values.
        assert published["ev_charger/sensor/l1_breaker_headroom/state"] == "50.0"
        assert published["ev_charger/sensor/grid_current_l1/state"] == "5.0"
        assert "ev_charger/sensor/uptime/state" in published


class TestMQTTPublishConfigStateNoneEnums:
    @pytest.mark.asyncio
    async def test_config_state_skips_unknown_runtime_enums(self):
        state = AppState(
            ev_advanced_charging_mode_enum=None,
            ev_plug_and_charge_auto_start_enum=None,
            ev_single_phase_switching_enum=None,
        )
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)
        client._client = AsyncMock()

        await client._publish_config_state()

        published = _published(client._client)
        assert "unavailable" not in published.values()
        assert "ev_charger/select/advanced_charging_mode/state" not in published
        assert "ev_charger/switch/plug_and_charge_auto_start/state" not in published
        assert "ev_charger/switch/single_phase_switching/state" not in published

        # Always-valid pairs still publish, with retain=True.
        client._client.publish.assert_any_await("ev_charger/select/mode/state", str(state.charge_mode), retain=True)
        client._client.publish.assert_any_await(
            "ev_charger/switch/eco_day_min_charge/state",
            "ON" if state.eco_day_min_charge_enabled else "OFF",
            retain=True,
        )


class TestMQTTRunLoopAvailability:
    @pytest.mark.asyncio
    async def test_run_loop_publishes_available_after_connect(self):
        state = AppState(mqtt_host="broker", mqtt_port=1883)
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)

        mqtt_client = AsyncMock()
        mqtt_context = AsyncMock()
        mqtt_context.__aenter__.return_value = mqtt_client
        mqtt_context.__aexit__.return_value = False

        class StopLoopError(Exception):
            pass

        async def _gather_and_stop(*aws):
            for aw in aws:
                await aw
            raise StopLoopError

        with (
            patch("app.ha.client._throttle"),
            patch("app.ha.client.aiomqtt.Client", return_value=mqtt_context) as client_ctor,
            patch.object(client, "_publish_discovery", new_callable=AsyncMock),
            patch.object(client, "_publish_config_state", new_callable=AsyncMock),
            patch.object(client, "_drain_queue", new_callable=AsyncMock),
            patch.object(client, "_process_messages", new_callable=AsyncMock),
            patch("app.ha.client.asyncio.gather", new=AsyncMock(side_effect=_gather_and_stop)),
            pytest.raises(StopLoopError),
        ):
            await client.run_loop()

        mqtt_client.publish.assert_any_await(AVAILABILITY_TOPIC, PAYLOAD_AVAILABLE, retain=True)
        # An LWT marking the device offline is configured on the client.
        assert "will" in client_ctor.call_args.kwargs
        will = client_ctor.call_args.kwargs["will"]
        assert will.topic == AVAILABILITY_TOPIC
        assert will.payload == PAYLOAD_NOT_AVAILABLE


class TestMQTTShutdownAvailability:
    @pytest.mark.asyncio
    async def test_shutdown_publishes_offline(self):
        state = AppState()
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue)
        client._client = AsyncMock()

        await client.shutdown()

        client._client.publish.assert_any_await(AVAILABILITY_TOPIC, PAYLOAD_NOT_AVAILABLE, retain=True)


class TestMQTTRuntimeSelectNoneConfirm:
    @pytest.mark.asyncio
    async def test_runtime_select_none_readback_skips_echo_and_disconnects(self):
        state = AppState(charge_mode="Standby")
        cfg = MagicMock()
        queue: asyncio.Queue = asyncio.Queue()
        ev = AsyncMock()
        ev.connected = True
        ev.ensure_connected = AsyncMock()
        ev.disconnect = AsyncMock()
        ev.write_advanced_charging_mode = AsyncMock(return_value=True)
        ev.read_advanced_charging_mode = AsyncMock(return_value=None)

        client = MQTTClient(state=state, config_manager=cfg, publish_queue=queue, ev_client=ev)
        client._client = AsyncMock()

        await client._handle_command("ev_charger/select/advanced_charging_mode/set", "PV charging")

        published = _published(client._client)
        assert "unavailable" not in published.values()
        assert "ev_charger/select/advanced_charging_mode/state" not in published
        # Standby exception path still disconnects.
        ev.disconnect.assert_awaited_once()
