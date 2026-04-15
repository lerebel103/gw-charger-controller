"""Home Assistant entity definitions and discovery metadata."""

from typing import Any

from app.ha.constants import PREFIX
from app.state import AdvancedChargingMode, ChargerStatus


def _sensor(
    unique_id: str,
    name: str,
    slug: str,
    unit: str | None,
    device_class: str | None = None,
    state_class: str | None = None,
    entity_category: str | None = None,
    options: list[str] | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "component": "sensor",
        "unique_id": unique_id,
        "name": name,
        "state_topic": f"{PREFIX}/sensor/{slug}/state",
        "force_update": True,
    }
    if unit is not None:
        d["unit_of_measurement"] = unit
    if device_class:
        d["device_class"] = device_class
    if state_class:
        d["state_class"] = state_class
    if entity_category:
        d["entity_category"] = entity_category
    if options:
        d["options"] = options
    return d


def _binary_sensor(
    unique_id: str,
    name: str,
    slug: str,
    device_class: str | None = None,
    entity_category: str | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "component": "binary_sensor",
        "unique_id": unique_id,
        "name": name,
        "state_topic": f"{PREFIX}/binary_sensor/{slug}/state",
    }
    if device_class:
        d["device_class"] = device_class
    if entity_category:
        d["entity_category"] = entity_category
    return d


def _switch(
    unique_id: str,
    name: str,
    slug: str,
    payload_on: str = "ON",
    payload_off: str = "OFF",
) -> dict[str, Any]:
    return {
        "component": "switch",
        "unique_id": unique_id,
        "name": name,
        "state_topic": f"{PREFIX}/switch/{slug}/state",
        "command_topic": f"{PREFIX}/switch/{slug}/set",
        "payload_on": payload_on,
        "payload_off": payload_off,
    }


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
        "state_topic": f"{PREFIX}/select/{slug}/state",
        "command_topic": f"{PREFIX}/select/{slug}/set",
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
        "state_topic": f"{PREFIX}/number/{slug}/state",
        "command_topic": f"{PREFIX}/number/{slug}/set",
        "min": min_val,
        "max": max_val,
        "step": step,
        "unit_of_measurement": unit,
    }
    if mode != "auto":
        d["mode"] = mode
    return d


def _text(unique_id: str, name: str, slug: str) -> dict[str, Any]:
    return {
        "component": "text",
        "unique_id": unique_id,
        "name": name,
        "state_topic": f"{PREFIX}/text/{slug}/state",
        "command_topic": f"{PREFIX}/text/{slug}/set",
    }


ENTITIES: list[dict[str, Any]] = [
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
    _sensor("ev_charger_status", "Charger Status", "status", None, "enum", None, None, ChargerStatus.ha_options()),
    _sensor("ev_charger_comm_connection_status", "Communication Connection Status", "comm_connection_status", None),
    _sensor("ev_charger_uptime", "Controller Uptime", "uptime", "s", None, "total_increasing", "diagnostic"),
    _binary_sensor("ev_charger_connected", "EV Connected", "connected", "connectivity"),
    _binary_sensor("ev_charger_comm_wifi_router", "Wi-Fi Router Connected", "comm_wifi_router", None, "diagnostic"),
    _binary_sensor("ev_charger_comm_iot_cloud", "IoT Cloud Connected", "comm_iot_cloud", None, "diagnostic"),
    _binary_sensor("ev_charger_comm_inverter", "Inverter Online", "comm_inverter", None, "diagnostic"),
    _binary_sensor("ev_charger_comm_mid_meter", "MID Meter Online", "comm_mid_meter", None, "diagnostic"),
    _binary_sensor("ev_charger_comm_gw_meter", "GW Meter Online", "comm_gw_meter", None, "diagnostic"),
    _binary_sensor("ev_charger_comm_ems", "EMS Online", "comm_ems", None, "diagnostic"),
    _select("ev_charger_mode", "Charge Mode", "mode", ["Standby", "Eco", "Manual"]),
    _select(
        "ev_charger_advanced_charging_mode",
        "Advanced Charging Mode",
        "advanced_charging_mode",
        AdvancedChargingMode.ha_options(),
    ),
    _switch("ev_charger_plug_and_charge_auto_start", "Plug and Charge Auto Start", "plug_and_charge_auto_start"),
    _switch("ev_charger_single_phase_switching", "Single Phase Switching", "single_phase_switching"),
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
    _number("victron_grid_meter_unit_id", "Victron Grid Meter Unit ID", "victron_grid_meter_unit_id", 1, 247, 1, ""),
    _number("ev_charger_control_loop_interval", "Control Loop Interval", "control_loop_interval", 1, 60, 1, "s"),
    _number("ev_charger_eco_mean_window", "Eco Mean Window", "eco_mean_window", 1, 10, 1, "min"),
    _number("ev_charger_solar_batt_day_limit", "Solar Batt Pwr Lim (day)", "solar_batt_day_limit", -10000, 0, 100, "W"),
    _number("ev_charger_eco_day_min_batt_soc", "Eco Day Min Batt SOC", "eco_day_min_batt_soc", 0, 100, 1, "%"),
    _number(
        "ev_charger_measurement_correction",
        "EV Measurement Correction",
        "measurement_correction",
        0,
        10,
        0.1,
        "%",
    ),
    _number("ev_charger_max_grid_power_draw", "Max Grid Power Draw", "max_grid_power_draw", 4200, 22000, 100, "W"),
    _text("ev_charger_solar_battery_discharge_start", "Solar Batt Discharge Start", "solar_battery_discharge_start"),
    _text("ev_charger_solar_battery_discharge_end", "Solar Batt Discharge End", "solar_battery_discharge_end"),
    _text("ev_charger_ip", "EV Charger IP", "ev_charger_ip"),
    _text("victron_ip", "Victron GX IP", "victron_ip"),
]

CMD_TO_STATE_TOPIC: dict[str, str] = {e["command_topic"]: e["state_topic"] for e in ENTITIES if "command_topic" in e}
