"""Constants and command metadata for Home Assistant MQTT integration."""

from app.state import AdvancedChargingMode

PREFIX = "ev_charger"
VEHICLE_SOC_TOPIC = f"{PREFIX}/vehicle/soc/set"

DEPRECATED_DISCOVERY_TOPICS = [
    "homeassistant/select/ev_charger_plug_and_charge_auto_start/config",
    "homeassistant/number/ev_charger_max_charging_power/config",
    "homeassistant/number/ev_charger_max_grid_power_draw/config",
]

# Maps command_topic -> (state_attr, value_type)
# value_type: "float", "int", "str", "select", "switch", "hhmm"
COMMAND_MAP: dict[str, tuple[str, str]] = {
    f"{PREFIX}/select/mode/set": ("charge_mode", "select"),
    f"{PREFIX}/select/advanced_charging_mode/set": ("ev_advanced_charging_mode", "select"),
    f"{PREFIX}/switch/plug_and_charge_auto_start/set": ("ev_plug_and_charge_auto_start", "switch"),
    f"{PREFIX}/switch/single_phase_switching/set": ("ev_single_phase_switching", "switch"),
    f"{PREFIX}/number/manual_power/set": ("manual_power_w", "float"),
    f"{PREFIX}/number/ev_min_soc/set": ("ev_min_soc_pct", "float"),
    f"{PREFIX}/number/ev_max_soc/set": ("ev_max_soc_pct", "float"),
    f"{PREFIX}/number/ev_battery_capacity/set": ("ev_battery_capacity_kwh", "float"),
    f"{PREFIX}/number/solar_battery_floor/set": ("solar_battery_discharge_floor_pct", "float"),
    f"{PREFIX}/number/solar_battery_max_ev_charge/set": ("solar_battery_max_ev_charge_power_w", "float"),
    f"{PREFIX}/number/solar_battery_max_discharge/set": ("solar_battery_max_discharge_w", "float"),
    f"{PREFIX}/number/ev_charger_port/set": ("ev_charger_port", "int"),
    f"{PREFIX}/number/victron_port/set": ("victron_port", "int"),
    f"{PREFIX}/number/victron_grid_meter_unit_id/set": ("victron_grid_meter_unit_id", "int"),
    f"{PREFIX}/number/control_loop_interval/set": ("control_loop_interval_s", "float"),
    f"{PREFIX}/number/eco_mean_window/set": ("eco_mean_window_minutes", "int"),
    f"{PREFIX}/number/solar_batt_day_limit/set": ("solar_battery_day_power_limit_w", "float"),
    f"{PREFIX}/number/eco_day_grid_export_charge_start/set": ("eco_day_grid_export_charge_start_w", "float"),
    f"{PREFIX}/number/eco_day_min_batt_soc/set": ("eco_day_min_solar_battery_soc_pct", "float"),
    f"{PREFIX}/number/eco_day_battery_full/set": ("eco_day_solar_battery_full_pct", "float"),
    f"{PREFIX}/number/eco_day_battery_full_exit/set": ("eco_day_solar_battery_full_exit_pct", "float"),
    f"{PREFIX}/number/eco_day_solar_batt_charge_start/set": ("eco_day_solar_battery_charge_start_w", "float"),
    f"{PREFIX}/number/eco_day_ramp_step/set": ("eco_day_ramp_step_w", "float"),
    f"{PREFIX}/number/measurement_correction/set": ("correction_pct", "float"),
    f"{PREFIX}/number/grid_breaker_limit/set": ("grid_breaker_limit_a", "float"),
    f"{PREFIX}/text/solar_battery_discharge_start/set": ("solar_battery_discharge_start", "hhmm"),
    f"{PREFIX}/text/solar_battery_discharge_end/set": ("solar_battery_discharge_end", "hhmm"),
    f"{PREFIX}/text/ev_charger_ip/set": ("ev_charger_ip", "str"),
    f"{PREFIX}/text/victron_ip/set": ("victron_ip", "str"),
}

NUMBER_RANGES: dict[str, tuple[float, float]] = {
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
    "eco_day_grid_export_charge_start_w": (-5000, -100),
    "eco_day_min_solar_battery_soc_pct": (0, 100),
    "eco_day_solar_battery_full_pct": (80, 100),
    "eco_day_solar_battery_full_exit_pct": (0, 100),
    "eco_day_solar_battery_charge_start_w": (2000, 8000),
    "eco_day_ramp_step_w": (10, 500),
    "correction_pct": (0, 10),
    "grid_breaker_limit_a": (10, 100),
}

SELECT_OPTIONS: dict[str, list[str]] = {
    "charge_mode": ["Standby", "Eco", "Manual"],
    "ev_advanced_charging_mode": AdvancedChargingMode.ha_options(),
}

RUNTIME_EV_COMMAND_FIELDS = {
    "ev_advanced_charging_mode",
    "ev_plug_and_charge_auto_start",
    "ev_single_phase_switching",
}

EV_RECONNECT_FIELDS = {"ev_charger_ip", "ev_charger_port"}
VICTRON_RECONNECT_FIELDS = {"victron_ip", "victron_port"}
