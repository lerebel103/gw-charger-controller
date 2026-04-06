"""MQTT client for Home Assistant discovery, state publishing, and command handling."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiomqtt

from app.backoff import exponential_backoff
from app.version import __version__
from app.config import ConfigManager
from app.control_loop import normalise_hhmm, validate_hhmm
from app.state import AppState, StateSnapshot

logger = logging.getLogger(__name__)

_PREFIX = "ev_charger"
_VEHICLE_SOC_TOPIC = f"{_PREFIX}/vehicle/soc/set"

_DEVICE = {
    "identifiers": ["ev_charger_integration"],
    "name": "EV Charger",
    "model": "GW22K-HCA-20",
    "manufacturer": "Goodwe",
    "sw_version": __version__,
}

# ---------------------------------------------------------------------------
# Entity definitions — single source of truth for discovery, state, commands
# ---------------------------------------------------------------------------

# Each entity: (component, unique_id, name, extra_discovery_fields)
# state_topic and command_topic are derived from component + slug.
# The slug is unique_id with the common prefix stripped where applicable.


def _sensor(
    unique_id: str,
    name: str,
    slug: str,
    unit: str,
    device_class: str | None = None,
    state_class: str | None = None,
    entity_category: str | None = None,
) -> dict[str, Any]:
    """Build a sensor entity definition."""
    d: dict[str, Any] = {
        "component": "sensor",
        "unique_id": unique_id,
        "name": name,
        "state_topic": f"{_PREFIX}/sensor/{slug}/state",
        "unit_of_measurement": unit,
        "force_update": True,
    }
    if device_class:
        d["device_class"] = device_class
    if state_class:
        d["state_class"] = state_class
    if entity_category:
        d["entity_category"] = entity_category
    return d


def _binary_sensor(
    unique_id: str,
    name: str,
    slug: str,
    device_class: str | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "component": "binary_sensor",
        "unique_id": unique_id,
        "name": name,
        "state_topic": f"{_PREFIX}/binary_sensor/{slug}/state",
    }
    if device_class:
        d["device_class"] = device_class
    return d


def _select(
    unique_id: str,
    name: str,
    slug: str,
    options: list[str],
) -> dict[str, Any]:
    return {
        "component": "select",
        "unique_id": unique_id,
        "name": name,
        "state_topic": f"{_PREFIX}/select/{slug}/state",
        "command_topic": f"{_PREFIX}/select/{slug}/set",
        "options": options,
    }


def _number(
    unique_id: str,
    name: str,
    slug: str,
    min_val: float,
    max_val: float,
    step: float,
    unit: str,
    mode: str = "box",
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "component": "number",
        "unique_id": unique_id,
        "name": name,
        "state_topic": f"{_PREFIX}/number/{slug}/state",
        "command_topic": f"{_PREFIX}/number/{slug}/set",
        "min": min_val,
        "max": max_val,
        "step": step,
        "unit_of_measurement": unit,
    }
    if mode != "auto":
        d["mode"] = mode
    return d


def _text(
    unique_id: str,
    name: str,
    slug: str,
) -> dict[str, Any]:
    return {
        "component": "text",
        "unique_id": unique_id,
        "name": name,
        "state_topic": f"{_PREFIX}/text/{slug}/state",
        "command_topic": f"{_PREFIX}/text/{slug}/set",
    }


# All entities registered with HA
ENTITIES: list[dict[str, Any]] = [
    # Sensors (read-only)
    _sensor("ev_charger_power", "EV Charger Power", "power", "W", "power", "measurement"),
    _sensor("ev_charger_session_energy", "Session Energy", "session_energy", "Wh", "energy", "total_increasing"),
    _sensor("ev_charger_total_energy", "EV Charger Total Energy", "total_energy", "Wh", "energy", "total_increasing"),
    _sensor("ev_charger_voltage_l1", "EV Voltage L1", "voltage_l1", "V", "voltage", "measurement"),
    _sensor("ev_charger_voltage_l2", "EV Voltage L2", "voltage_l2", "V", "voltage", "measurement"),
    _sensor("ev_charger_voltage_l3", "EV Voltage L3", "voltage_l3", "V", "voltage", "measurement"),
    _sensor("ev_charger_current_l1", "EV Current L1", "current_l1", "A", "current", "measurement"),
    _sensor("ev_charger_current_l2", "EV Current L2", "current_l2", "A", "current", "measurement"),
    _sensor("ev_charger_current_l3", "EV Current L3", "current_l3", "A", "current", "measurement"),
    _sensor("ev_charger_setpoint", "Charge Setpoint", "setpoint", "W", "power", "measurement"),
    _sensor("ev_charger_l1_voltage_drop", "L1 Voltage Drop %", "l1_voltage_drop_perc", "%", None, "measurement"),
    _sensor("ev_charger_l2_voltage_drop", "L2 Voltage Drop %", "l2_voltage_drop_perc", "%", None, "measurement"),
    _sensor("ev_charger_l3_voltage_drop", "L3 Voltage Drop %", "l3_voltage_drop_perc", "%", None, "measurement"),
    _sensor("ev_charger_completion_time", "Completion Time", "completion_time", "h", None, "measurement"),
    _sensor("ev_charger_soc", "EV SOC", "ev_soc", "%", "battery", "measurement"),
    _sensor("ev_charger_uptime", "Controller Uptime", "uptime", "s", None, "total_increasing", "diagnostic"),
    # Binary sensors
    _binary_sensor("ev_charger_connected", "EV Connected", "connected", "connectivity"),
    # Select
    _select("ev_charger_mode", "Charge Mode", "mode", ["Eco", "Manual", "Standby"]),
    # Numbers
    _number("ev_charger_manual_power", "Manual Charge Power", "manual_power", 4400, 11000, 100, "W"),
    _number("ev_charger_ev_min_soc", "Min EV SOC", "ev_min_soc", 0, 100, 1, "%"),
    _number("ev_charger_ev_max_soc", "Max EV SOC", "ev_max_soc", 80, 100, 1, "%"),
    _number("ev_charger_ev_battery_capacity", "EV Battery Capacity", "ev_battery_capacity", 10, 200, 1, "kWh"),
    _number("ev_charger_solar_battery_floor", "Solar Batt Discharge Floor", "solar_battery_floor", 0, 100, 1, "%"),
    _number(
        "ev_charger_solar_battery_max_ev_charge",
        "EV Charge Power (Batt Window)",
        "solar_battery_max_ev_charge",
        4400,
        11000,
        100,
        "W",
    ),
    _number(
        "ev_charger_solar_battery_max_discharge",
        "Solar Batt Max Discharge",
        "solar_battery_max_discharge",
        0,
        15000,
        100,
        "W",
    ),
    _number("ev_charger_port", "EV Charger Port", "ev_charger_port", 1, 65535, 1, ""),
    _number("victron_port", "Victron GX Port", "victron_port", 1, 65535, 1, ""),
    _number(
        "victron_grid_meter_unit_id",
        "Victron Grid Meter Unit ID",
        "victron_grid_meter_unit_id",
        1,
        247,
        1,
        "",
    ),
    _number(
        "ev_charger_control_loop_interval",
        "Control Loop Interval",
        "control_loop_interval",
        1,
        60,
        1,
        "s",
    ),
    _number(
        "ev_charger_eco_mean_window",
        "Eco Mean Window",
        "eco_mean_window",
        1,
        10,
        1,
        "min",
    ),
    _number(
        "ev_charger_solar_batt_day_limit",
        "Solar Batt Pwr Lim (day)",
        "solar_batt_day_limit",
        -10000,
        0,
        100,
        "W",
    ),
    _number(
        "ev_charger_eco_day_min_batt_soc",
        "Eco Day Min Batt SOC",
        "eco_day_min_batt_soc",
        0,
        100,
        1,
        "%",
    ),
    # Text
    _text("ev_charger_solar_battery_discharge_start", "Solar Batt Discharge Start", "solar_battery_discharge_start"),
    _text("ev_charger_solar_battery_discharge_end", "Solar Batt Discharge End", "solar_battery_discharge_end"),
    _text("ev_charger_ip", "EV Charger IP", "ev_charger_ip"),
    _text("victron_ip", "Victron GX IP", "victron_ip"),
]


# ---------------------------------------------------------------------------
# Command topic → (AppState field, type, validation) mapping
# ---------------------------------------------------------------------------

# Maps command_topic → (state_attr, value_type)
# value_type: "float", "int", "str", "select", "hhmm"
_COMMAND_MAP: dict[str, tuple[str, str]] = {
    f"{_PREFIX}/select/mode/set": ("charge_mode", "select"),
    f"{_PREFIX}/number/manual_power/set": ("manual_power_w", "float"),
    f"{_PREFIX}/number/ev_min_soc/set": ("ev_min_soc_pct", "float"),
    f"{_PREFIX}/number/ev_max_soc/set": ("ev_max_soc_pct", "float"),
    f"{_PREFIX}/number/ev_battery_capacity/set": ("ev_battery_capacity_kwh", "float"),
    f"{_PREFIX}/number/solar_battery_floor/set": ("solar_battery_discharge_floor_pct", "float"),
    f"{_PREFIX}/number/solar_battery_max_ev_charge/set": ("solar_battery_max_ev_charge_power_w", "float"),
    f"{_PREFIX}/number/solar_battery_max_discharge/set": ("solar_battery_max_discharge_w", "float"),
    f"{_PREFIX}/number/ev_charger_port/set": ("ev_charger_port", "int"),
    f"{_PREFIX}/number/victron_port/set": ("victron_port", "int"),
    f"{_PREFIX}/number/victron_grid_meter_unit_id/set": ("victron_grid_meter_unit_id", "int"),
    f"{_PREFIX}/number/control_loop_interval/set": ("control_loop_interval_s", "float"),
    f"{_PREFIX}/number/eco_mean_window/set": ("eco_mean_window_minutes", "int"),
    f"{_PREFIX}/number/solar_batt_day_limit/set": ("solar_battery_day_power_limit_w", "float"),
    f"{_PREFIX}/number/eco_day_min_batt_soc/set": ("eco_day_min_battery_soc_pct", "float"),
    f"{_PREFIX}/text/solar_battery_discharge_start/set": ("solar_battery_discharge_start", "hhmm"),
    f"{_PREFIX}/text/solar_battery_discharge_end/set": ("solar_battery_discharge_end", "hhmm"),
    f"{_PREFIX}/text/ev_charger_ip/set": ("ev_charger_ip", "str"),
    f"{_PREFIX}/text/victron_ip/set": ("victron_ip", "str"),
}

# Number entity ranges for validation: state_attr → (min, max)
_NUMBER_RANGES: dict[str, tuple[float, float]] = {
    "manual_power_w": (4400, 11000),
    "ev_min_soc_pct": (0, 100),
    "ev_max_soc_pct": (80, 100),
    "ev_battery_capacity_kwh": (10, 200),
    "solar_battery_discharge_floor_pct": (0, 100),
    "solar_battery_max_ev_charge_power_w": (4400, 11000),
    "solar_battery_max_discharge_w": (0, 15000),
    "ev_charger_port": (1, 65535),
    "victron_port": (1, 65535),
    "victron_grid_meter_unit_id": (1, 247),
    "control_loop_interval_s": (1, 60),
    "eco_mean_window_minutes": (1, 10),
    "solar_battery_day_power_limit_w": (-10000, 0),
    "eco_day_min_battery_soc_pct": (0, 100),
}

# Select entity valid options
_SELECT_OPTIONS: dict[str, list[str]] = {
    "charge_mode": ["Eco", "Manual", "Standby"],
}

# Build reverse map: command_topic → state_topic (from ENTITIES)
_CMD_TO_STATE_TOPIC: dict[str, str] = {e["command_topic"]: e["state_topic"] for e in ENTITIES if "command_topic" in e}

# Fields that trigger a Modbus client reconnect
_EV_RECONNECT_FIELDS = {"ev_charger_ip", "ev_charger_port"}
_VICTRON_RECONNECT_FIELDS = {"victron_ip", "victron_port"}


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

    # ------------------------------------------------------------------
    # Task 8.1 — Discovery
    # ------------------------------------------------------------------

    async def _publish_discovery(self) -> None:
        """Publish HA MQTT discovery payloads for all entities."""
        assert self._client is not None  # noqa: S101
        for entity in ENTITIES:
            component = entity["component"]
            unique_id = entity["unique_id"]
            topic = f"homeassistant/{component}/{unique_id}/config"

            payload: dict[str, Any] = {
                "name": entity["name"],
                "unique_id": unique_id,
                "object_id": unique_id,
                "state_topic": entity["state_topic"],
                "device": _DEVICE,
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
            for key in ("min", "max", "step", "mode", "force_update", "entity_category"):
                if key in entity:
                    payload[key] = entity[key]

            await self._client.publish(topic, json.dumps(payload), retain=True)

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

        # Binary sensor
        await self._client.publish(
            f"{_PREFIX}/binary_sensor/connected/state",
            "ON" if snapshot.ev_connected else "OFF",
        )

        # Diagnostics
        await self._client.publish(
            f"{_PREFIX}/sensor/uptime/state", str(int(snapshot.uptime_s))
        )

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
            (f"{_PREFIX}/text/solar_battery_discharge_start/state", s.solar_battery_discharge_start),
            (f"{_PREFIX}/text/solar_battery_discharge_end/state", s.solar_battery_discharge_end),
            (f"{_PREFIX}/text/ev_charger_ip/state", s.ev_charger_ip),
            (f"{_PREFIX}/text/victron_ip/state", s.victron_ip),
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

        if vtype == "select":
            valid_options = _SELECT_OPTIONS.get(attr, [])
            if payload not in valid_options:
                logger.warning("Invalid select value '%s' for %s", payload, attr)
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
