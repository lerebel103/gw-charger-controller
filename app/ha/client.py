"""MQTT client for Home Assistant discovery, state publishing, and command handling."""

import asyncio
import json
import logging
import time
from typing import Any

import aiomqtt

from app.backoff import exponential_backoff
from app.config import ConfigManager
from app.control import normalise_hhmm, validate_hhmm
from app.ha.constants import (
    COMMAND_MAP as _COMMAND_MAP,
)
from app.ha.constants import (
    DEPRECATED_DISCOVERY_TOPICS as _DEPRECATED_DISCOVERY_TOPICS,
)
from app.ha.constants import (
    EV_RECONNECT_FIELDS as _EV_RECONNECT_FIELDS,
)
from app.ha.constants import (
    NUMBER_RANGES as _NUMBER_RANGES,
)
from app.ha.constants import (
    PREFIX as _PREFIX,
)
from app.ha.constants import (
    RUNTIME_EV_COMMAND_FIELDS as _RUNTIME_EV_COMMAND_FIELDS,
)
from app.ha.constants import (
    SELECT_OPTIONS as _SELECT_OPTIONS,
)
from app.ha.constants import (
    VEHICLE_SOC_TOPIC as _VEHICLE_SOC_TOPIC,
)
from app.ha.constants import (
    VICTRON_RECONNECT_FIELDS as _VICTRON_RECONNECT_FIELDS,
)
from app.ha.device import device_payload as _device_payload
from app.ha.entities import CMD_TO_STATE_TOPIC as _CMD_TO_STATE_TOPIC
from app.ha.entities import ENTITIES
from app.ha.parsers import (
    parse_advanced_mode_payload as _parse_advanced_mode_payload,
)
from app.ha.parsers import (
    parse_max_grid_power_draw_payload as _parse_max_grid_power_draw_payload,
)
from app.ha.parsers import (
    parse_plug_and_charge_payload as _parse_plug_and_charge_payload,
)
from app.ha.parsers import (
    parse_single_phase_payload as _parse_single_phase_payload,
)
from app.state import (
    AdvancedChargingMode,
    AppState,
    PlugAndChargeAutoStart,
    SinglePhaseSwitching,
    StateSnapshot,
)

logger = logging.getLogger(__name__)


class MQTTClient:
    """Manages MQTT connection, HA discovery, state publishing, and commands."""

    def __init__(
        self,
        state: AppState,
        config_manager: ConfigManager,
        publish_queue: asyncio.Queue,
        victron_client: Any | None = None,
        ev_client: Any | None = None,
    ) -> None:
        self._state = state
        self._config_manager = config_manager
        self._publish_queue: asyncio.Queue = publish_queue
        self._victron_client = victron_client
        self._ev_client = ev_client
        self._client: aiomqtt.Client | None = None
        self._published_device_serial: str | None = None

    # ------------------------------------------------------------------
    # Task 8.1 — Discovery
    # ------------------------------------------------------------------

    async def _publish_discovery(self) -> None:
        """Publish HA MQTT discovery payloads for all entities."""
        assert self._client is not None  # noqa: S101

        for topic in _DEPRECATED_DISCOVERY_TOPICS:
            await self._client.publish(topic, "", retain=True)

        for entity in ENTITIES:
            component = entity["component"]
            unique_id = entity["unique_id"]
            topic = f"homeassistant/{component}/{unique_id}/config"

            payload: dict[str, Any] = {
                "name": entity["name"],
                "unique_id": unique_id,
                "object_id": unique_id,
                "state_topic": entity["state_topic"],
                "device": _device_payload(self._state),
            }

            # Optional fields
            if "unit_of_measurement" in entity:
                payload["unit_of_measurement"] = entity["unit_of_measurement"]
            if "device_class" in entity:
                payload["device_class"] = entity["device_class"]
            if "state_class" in entity:
                payload["state_class"] = entity["state_class"]
            if "command_topic" in entity:
                payload["command_topic"] = entity["command_topic"]
            if "options" in entity:
                payload["options"] = entity["options"]
            if "payload_on" in entity:
                payload["payload_on"] = entity["payload_on"]
            if "payload_off" in entity:
                payload["payload_off"] = entity["payload_off"]
            for key in ("min", "max", "step", "mode", "force_update", "entity_category"):
                if key in entity:
                    payload[key] = entity[key]

            await self._client.publish(topic, json.dumps(payload), retain=True)

        self._published_device_serial = self._state.ev_serial_number

    # ------------------------------------------------------------------
    # Task 8.3 — State publishing
    # ------------------------------------------------------------------

    async def _publish_state(self, snapshot: StateSnapshot) -> None:
        """Publish all sensor values from a StateSnapshot to their state topics."""
        assert self._client is not None  # noqa: S101

        def _fmt(value: Any) -> str:
            if value is None:
                return "unavailable"
            return str(value)

        def _fmt_drop(value: float | None) -> str:
            if value is None:
                return "unavailable"
            return str(round(value, 2))

        def _fmt_binary(value: bool | None) -> str:
            if value is None:
                return "unavailable"
            return "ON" if value else "OFF"

        # Sensors
        await self._client.publish(f"{_PREFIX}/sensor/power/state", _fmt(snapshot.ev_active_power_w))
        await self._client.publish(f"{_PREFIX}/sensor/session_energy/state", _fmt(snapshot.ev_session_energy_wh))
        await self._client.publish(f"{_PREFIX}/sensor/total_energy/state", _fmt(snapshot.ev_total_energy_wh))
        await self._client.publish(f"{_PREFIX}/sensor/voltage_l1/state", _fmt(snapshot.ev_voltage_l1_v))
        await self._client.publish(f"{_PREFIX}/sensor/voltage_l2/state", _fmt(snapshot.ev_voltage_l2_v))
        await self._client.publish(f"{_PREFIX}/sensor/voltage_l3/state", _fmt(snapshot.ev_voltage_l3_v))
        await self._client.publish(f"{_PREFIX}/sensor/current_l1/state", _fmt(snapshot.ev_current_a))
        await self._client.publish(f"{_PREFIX}/sensor/current_l2/state", _fmt(snapshot.ev_current_b))
        await self._client.publish(f"{_PREFIX}/sensor/current_l3/state", _fmt(snapshot.ev_current_c))
        await self._client.publish(f"{_PREFIX}/sensor/setpoint/state", _fmt(snapshot.commanded_setpoint_w))
        await self._client.publish(
            f"{_PREFIX}/sensor/l1_voltage_drop_perc/state",
            _fmt_drop(snapshot.l1_voltage_drop_pct),
        )
        await self._client.publish(
            f"{_PREFIX}/sensor/l2_voltage_drop_perc/state",
            _fmt_drop(snapshot.l2_voltage_drop_pct),
        )
        await self._client.publish(
            f"{_PREFIX}/sensor/l3_voltage_drop_perc/state",
            _fmt_drop(snapshot.l3_voltage_drop_pct),
        )
        await self._client.publish(f"{_PREFIX}/sensor/completion_time/state", _fmt(snapshot.ev_completion_time_h))
        await self._client.publish(f"{_PREFIX}/sensor/ev_soc/state", _fmt(snapshot.ev_soc_pct))
        await self._client.publish(f"{_PREFIX}/sensor/status/state", _fmt(snapshot.ev_charger_status_display))
        await self._client.publish(
            f"{_PREFIX}/sensor/comm_connection_status/state",
            _fmt(snapshot.ev_comm_connection_status_raw),
        )
        await self._client.publish(
            f"{_PREFIX}/select/advanced_charging_mode/state",
            _fmt(snapshot.ev_advanced_charging_mode_display),
            retain=True,
        )

        # Binary sensor
        await self._client.publish(
            f"{_PREFIX}/binary_sensor/connected/state",
            "ON" if snapshot.ev_connected else "OFF",
        )
        await self._client.publish(
            f"{_PREFIX}/binary_sensor/comm_wifi_router/state",
            _fmt_binary(snapshot.ev_comm_wifi_router_connected),
        )
        await self._client.publish(
            f"{_PREFIX}/binary_sensor/comm_iot_cloud/state",
            _fmt_binary(snapshot.ev_comm_iot_cloud_connected),
        )
        await self._client.publish(
            f"{_PREFIX}/binary_sensor/comm_inverter/state",
            _fmt_binary(snapshot.ev_comm_inverter_online),
        )
        await self._client.publish(
            f"{_PREFIX}/binary_sensor/comm_mid_meter/state",
            _fmt_binary(snapshot.ev_comm_mid_meter_online),
        )
        await self._client.publish(
            f"{_PREFIX}/binary_sensor/comm_gw_meter/state",
            _fmt_binary(snapshot.ev_comm_gw_meter_online),
        )
        await self._client.publish(
            f"{_PREFIX}/binary_sensor/comm_ems/state",
            _fmt_binary(snapshot.ev_comm_ems_online),
        )

        # Switch
        switch_state = "unavailable"
        if snapshot.ev_single_phase_switching_display == "Enabled":
            switch_state = "ON"
        elif snapshot.ev_single_phase_switching_display == "Disabled":
            switch_state = "OFF"
        await self._client.publish(
            f"{_PREFIX}/switch/single_phase_switching/state",
            switch_state,
            retain=True,
        )
        plug_and_charge_state = "unavailable"
        if snapshot.ev_plug_and_charge_auto_start_display == "On":
            plug_and_charge_state = "ON"
        elif snapshot.ev_plug_and_charge_auto_start_display == "Off":
            plug_and_charge_state = "OFF"
        await self._client.publish(
            f"{_PREFIX}/switch/plug_and_charge_auto_start/state",
            plug_and_charge_state,
            retain=True,
        )
        await self._client.publish(
            f"{_PREFIX}/number/max_grid_power_draw/state",
            _fmt(snapshot.ev_max_grid_power_draw_w),
            retain=True,
        )

        # Diagnostics
        await self._client.publish(f"{_PREFIX}/sensor/uptime/state", str(int(snapshot.uptime_s)))

    async def _publish_config_state(self) -> None:
        """Publish current config/control values to their state topics."""
        assert self._client is not None  # noqa: S101
        s = self._state
        pairs: list[tuple[str, str]] = [
            (f"{_PREFIX}/select/mode/state", str(s.charge_mode)),
            (f"{_PREFIX}/number/manual_power/state", str(s.manual_power_w)),
            (f"{_PREFIX}/number/ev_min_soc/state", str(s.ev_min_soc_pct)),
            (f"{_PREFIX}/number/ev_max_soc/state", str(s.ev_max_soc_pct)),
            (f"{_PREFIX}/number/ev_battery_capacity/state", str(s.ev_battery_capacity_kwh)),
            (f"{_PREFIX}/number/solar_battery_floor/state", str(s.solar_battery_discharge_floor_pct)),
            (f"{_PREFIX}/number/solar_battery_max_ev_charge/state", str(s.solar_battery_max_ev_charge_power_w)),
            (f"{_PREFIX}/number/solar_battery_max_discharge/state", str(s.solar_battery_max_discharge_w)),
            (f"{_PREFIX}/number/ev_charger_port/state", str(s.ev_charger_port)),
            (f"{_PREFIX}/number/victron_port/state", str(s.victron_port)),
            (f"{_PREFIX}/number/victron_grid_meter_unit_id/state", str(s.victron_grid_meter_unit_id)),
            (f"{_PREFIX}/number/control_loop_interval/state", str(s.control_loop_interval_s)),
            (f"{_PREFIX}/number/eco_mean_window/state", str(s.eco_mean_window_minutes)),
            (f"{_PREFIX}/number/solar_batt_day_limit/state", str(s.solar_battery_day_power_limit_w)),
            (f"{_PREFIX}/number/eco_day_min_batt_soc/state", str(s.eco_day_min_battery_soc_pct)),
            (f"{_PREFIX}/number/eco_day_ramp_step/state", str(s.eco_day_ramp_step_w)),
            (f"{_PREFIX}/number/measurement_correction/state", str(s.correction_pct)),
            (f"{_PREFIX}/text/solar_battery_discharge_start/state", s.solar_battery_discharge_start),
            (f"{_PREFIX}/text/solar_battery_discharge_end/state", s.solar_battery_discharge_end),
            (f"{_PREFIX}/text/ev_charger_ip/state", s.ev_charger_ip),
            (f"{_PREFIX}/text/victron_ip/state", s.victron_ip),
            (
                f"{_PREFIX}/select/advanced_charging_mode/state",
                s.ev_advanced_charging_mode_enum.display_name
                if s.ev_advanced_charging_mode_enum is not None
                else "unavailable",
            ),
            (
                f"{_PREFIX}/switch/plug_and_charge_auto_start/state",
                "ON"
                if s.ev_plug_and_charge_auto_start_enum == PlugAndChargeAutoStart.ON
                else "OFF"
                if s.ev_plug_and_charge_auto_start_enum == PlugAndChargeAutoStart.OFF
                else "unavailable",
            ),
            (
                f"{_PREFIX}/switch/single_phase_switching/state",
                "ON"
                if s.ev_single_phase_switching_enum == SinglePhaseSwitching.ENABLED
                else "OFF"
                if s.ev_single_phase_switching_enum == SinglePhaseSwitching.DISABLED
                else "unavailable",
            ),
            (
                f"{_PREFIX}/number/max_grid_power_draw/state",
                str(s.ev_max_grid_power_draw_raw * 100.0)
                if s.ev_max_grid_power_draw_raw is not None
                else "unavailable",
            ),
        ]
        for topic, value in pairs:
            await self._client.publish(topic, value, retain=True)

    # ------------------------------------------------------------------
    # Task 8.5 — Command handling
    # ------------------------------------------------------------------

    async def _handle_command(self, topic: str, payload: str) -> None:
        """Validate payload, update AppState, persist, and trigger reconnects."""
        topic_str = str(topic)

        # Handle external vehicle SOC input (not a config entity)
        if topic_str == _VEHICLE_SOC_TOPIC:
            try:
                soc = float(payload)
            except (ValueError, TypeError):
                logger.warning("Invalid vehicle SOC value: %s", payload)
                return
            if not (0 <= soc <= 100):
                logger.warning("Vehicle SOC out of range [0-100]: %s", soc)
                return
            self._state.ev_soc_pct = soc
            self._state.ev_soc_pct_updated_at = time.monotonic()
            logger.debug("Received vehicle SOC: %.1f%%", soc)
            return

        mapping = _COMMAND_MAP.get(topic_str)
        if mapping is None:
            logger.warning("Unknown command topic: %s", topic_str)
            return

        attr, vtype = mapping

        if attr in _RUNTIME_EV_COMMAND_FIELDS:
            await self._handle_runtime_ev_select(attr, payload, topic_str)
            return

        if vtype == "select":
            valid_options = _SELECT_OPTIONS.get(attr, [])
            if payload not in valid_options:
                logger.warning("Invalid select value '%s' for %s", payload, attr)
                return
            if getattr(self._state, attr) == payload:
                return
            setattr(self._state, attr, payload)

        elif vtype == "hhmm":
            if not validate_hhmm(payload):
                logger.error("Invalid HH:MM value '%s' for %s, retaining previous value", payload, attr)
                return
            setattr(self._state, attr, normalise_hhmm(payload))

        elif vtype == "float":
            try:
                val = float(payload)
            except (ValueError, TypeError):
                logger.warning("Invalid float value '%s' for %s", payload, attr)
                return
            rng = _NUMBER_RANGES.get(attr)
            if rng and not (rng[0] <= val <= rng[1]):
                logger.warning("Value %s out of range %s for %s", val, rng, attr)
                return
            setattr(self._state, attr, val)

        elif vtype == "int":
            try:
                val_i = int(float(payload))
            except (ValueError, TypeError):
                logger.warning("Invalid int value '%s' for %s", payload, attr)
                return
            rng = _NUMBER_RANGES.get(attr)
            if rng and not (rng[0] <= val_i <= rng[1]):
                logger.warning("Value %s out of range %s for %s", val_i, rng, attr)
                return
            setattr(self._state, attr, val_i)

        elif vtype == "str":
            setattr(self._state, attr, payload)

        else:
            logger.warning("Unknown value type '%s' for %s", vtype, attr)
            return

        # Persist
        self._config_manager.schedule_persist(self._state)

        # Republish the updated value to the state topic so HA confirms the change
        state_topic = _CMD_TO_STATE_TOPIC.get(topic_str)
        if state_topic and self._client is not None:
            new_value = str(getattr(self._state, attr))
            await self._client.publish(state_topic, new_value, retain=True)

        # Trigger reconnect for device connection changes
        if attr in _EV_RECONNECT_FIELDS and self._ev_client is not None:
            logger.info("EV charger connection config changed (%s), triggering reconnect", attr)
            asyncio.ensure_future(self._ev_client.reconnect())

        if attr in _VICTRON_RECONNECT_FIELDS and self._victron_client is not None:
            logger.info("Victron GX connection config changed (%s), triggering reconnect", attr)
            asyncio.ensure_future(self._victron_client.reconnect())

    async def _handle_runtime_ev_select(self, attr: str, payload: str, topic: str) -> None:
        """Handle user-driven runtime EV select commands, including standby exception path."""
        if self._ev_client is None:
            logger.warning("Runtime EV command ignored (%s): EV client unavailable", attr)
            return

        enum_value: AdvancedChargingMode | PlugAndChargeAutoStart | SinglePhaseSwitching | None = None
        number_value_w: float | None = None

        if attr == "ev_advanced_charging_mode":
            enum_value = _parse_advanced_mode_payload(payload)
            if enum_value is None:
                logger.warning("Invalid select value '%s' for %s", payload, attr)
                return
            if self._state.ev_advanced_charging_mode_enum == enum_value:
                return
            writer = self._ev_client.write_advanced_charging_mode
            reader = self._ev_client.read_advanced_charging_mode
        elif attr == "ev_plug_and_charge_auto_start":
            enum_value = _parse_plug_and_charge_payload(payload)
            if enum_value is None:
                logger.warning("Invalid switch value '%s' for %s", payload, attr)
                return
            if self._state.ev_plug_and_charge_auto_start_enum == enum_value:
                return
            writer = self._ev_client.write_plug_and_charge_auto_start
            reader = self._ev_client.read_plug_and_charge_auto_start
        elif attr == "ev_single_phase_switching":
            enum_value = _parse_single_phase_payload(payload)
            if enum_value is None:
                logger.warning("Invalid switch value '%s' for %s", payload, attr)
                return
            if self._state.ev_single_phase_switching_enum == enum_value:
                return
            writer = self._ev_client.write_single_phase_switching
            reader = self._ev_client.read_single_phase_switching
        elif attr == "ev_max_grid_power_draw":
            number_value_w = _parse_max_grid_power_draw_payload(payload)
            if number_value_w is None:
                logger.warning("Invalid number value '%s' for %s", payload, attr)
                return
            current_w = (
                self._state.ev_max_grid_power_draw_raw * 100.0
                if self._state.ev_max_grid_power_draw_raw is not None
                else None
            )
            if current_w is not None and abs(current_w - number_value_w) < 0.5:
                return
            writer = self._ev_client.write_max_grid_power_draw
            reader = self._ev_client.read_max_grid_power_draw
        else:
            logger.warning("Unsupported runtime EV select field: %s", attr)
            return

        standby_override = self._state.charge_mode == "Standby"

        await self._ev_client.ensure_connected()
        if not self._ev_client.connected:
            logger.warning("Runtime EV command ignored (%s): unable to connect to charger", attr)
            return

        try:
            if attr == "ev_max_grid_power_draw":
                ok = await writer(number_value_w)
            else:
                ok = await writer(enum_value)
            if not ok:
                return
            confirmed = await reader()
            if attr == "ev_single_phase_switching":
                state_value = "ON" if confirmed == SinglePhaseSwitching.ENABLED else "OFF"
            elif attr == "ev_plug_and_charge_auto_start":
                if confirmed == PlugAndChargeAutoStart.ON:
                    state_value = "ON"
                elif confirmed == PlugAndChargeAutoStart.OFF:
                    state_value = "OFF"
                else:
                    state_value = "unavailable"
            elif attr == "ev_max_grid_power_draw":
                state_value = str(confirmed) if confirmed is not None else "unavailable"
            else:
                state_value = confirmed.display_name if confirmed is not None else "unavailable"

            state_topic = _CMD_TO_STATE_TOPIC.get(topic)
            if state_topic and self._client is not None:
                await self._client.publish(state_topic, state_value, retain=True)
        finally:
            if standby_override:
                await self._ev_client.disconnect()

    # ------------------------------------------------------------------
    # Task 8.7 — Run loop
    # ------------------------------------------------------------------

    async def run_loop(self) -> None:
        """Connect to MQTT broker with backoff, publish discovery, handle messages."""
        attempt = 0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self._state.mqtt_host,
                    port=self._state.mqtt_port,
                    username=self._state.mqtt_username,
                    password=self._state.mqtt_password,
                ) as client:
                    self._client = client
                    attempt = 0
                    logger.info(
                        "Connected to MQTT broker at %s:%d",
                        self._state.mqtt_host,
                        self._state.mqtt_port,
                    )

                    # Publish discovery and subscribe to command topics
                    await self._publish_discovery()
                    for entity in ENTITIES:
                        cmd = entity.get("command_topic")
                        if cmd:
                            await client.subscribe(cmd)

                    # Subscribe to external vehicle SOC input
                    await client.subscribe(_VEHICLE_SOC_TOPIC)

                    # Publish current config state
                    await self._publish_config_state()

                    # Concurrently drain publish_queue and process incoming messages
                    await asyncio.gather(
                        self._drain_queue(),
                        self._process_messages(),
                    )

            except aiomqtt.MqttError as exc:
                logger.warning("MQTT connection error: %s", exc)
            finally:
                self._client = None

            delay = exponential_backoff(attempt)
            logger.info("Retrying MQTT connection in %.1f s", delay)
            await asyncio.sleep(delay)
            attempt += 1

    async def _drain_queue(self) -> None:
        """Continuously drain the publish_queue and publish state/events."""
        assert self._client is not None  # noqa: S101
        while True:
            item = await self._publish_queue.get()
            try:
                if item == "republish_config":
                    await self._publish_config_state()
                elif isinstance(item, dict) and item.get("type") == "charging_event":
                    await self._publish_charging_event(item)
                elif isinstance(item, StateSnapshot):
                    if item.ev_serial_number and item.ev_serial_number != self._published_device_serial:
                        await self._publish_discovery()
                    await self._publish_state(item)
            except aiomqtt.MqttError:
                logger.warning("Failed to publish from queue")
                raise

    async def _process_messages(self) -> None:
        """Process incoming MQTT messages (commands)."""
        assert self._client is not None  # noqa: S101
        async for message in self._client.messages:
            try:
                payload = message.payload
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8")
                await self._handle_command(str(message.topic), str(payload))
            except Exception:
                logger.exception("Error handling MQTT message on %s", message.topic)

    async def _publish_charging_event(self, event: dict) -> None:
        """Publish a charging event to ev_charger/event/charging."""
        assert self._client is not None  # noqa: S101
        payload = {k: v for k, v in event.items() if k != "type"}
        await self._client.publish(
            f"{_PREFIX}/event/charging",
            json.dumps(payload),
        )
        logger.info("Published charging event: %s", payload.get("event"))

    async def shutdown(self) -> None:
        """Publish empty payloads to all discovery topics for graceful removal."""
        if self._client is None:
            return
        for entity in ENTITIES:
            component = entity["component"]
            unique_id = entity["unique_id"]
            topic = f"homeassistant/{component}/{unique_id}/config"
            try:
                await self._client.publish(topic, "", retain=True)
            except aiomqtt.MqttError:
                logger.warning("Failed to clear discovery for %s", unique_id)
