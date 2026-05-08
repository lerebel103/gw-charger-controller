"""State dataclasses and persisted-field definitions."""

from dataclasses import dataclass, field
from datetime import datetime

from app.state.enums import AdvancedChargingMode, ChargerStatus, PlugAndChargeAutoStart, SinglePhaseSwitching


@dataclass
class AppState:
    """Central in-memory state. Single-threaded asyncio - no locking needed."""

    # Victron GX readings
    grid_power_w: float | None = None
    solar_battery_power_w: float | None = None
    solar_battery_soc_pct: float | None = None
    victron_l1_voltage_v: float | None = None
    victron_l2_voltage_v: float | None = None
    victron_l3_voltage_v: float | None = None

    # EV charger readings
    ev_connected: bool = False
    ev_charger_status: int | None = None
    ev_charger_status_enum: ChargerStatus | None = None
    ev_comm_connection_status_raw: int | None = None
    ev_comm_wifi_router_connected: bool | None = None
    ev_comm_iot_cloud_connected: bool | None = None
    ev_comm_inverter_online: bool | None = None
    ev_comm_mid_meter_online: bool | None = None
    ev_comm_gw_meter_online: bool | None = None
    ev_comm_ems_online: bool | None = None
    ev_serial_number: str | None = None
    ev_advanced_charging_mode: int | None = None
    ev_advanced_charging_mode_enum: AdvancedChargingMode | None = None
    ev_single_phase_switching: int | None = None
    ev_single_phase_switching_enum: SinglePhaseSwitching | None = None
    ev_active_power_w: float | None = None
    ev_session_energy_wh: float | None = None
    ev_voltage_l1_v: float | None = None
    ev_voltage_l2_v: float | None = None
    ev_voltage_l3_v: float | None = None
    ev_current_a: float | None = None
    ev_current_b: float | None = None
    ev_current_c: float | None = None
    ev_completion_time_h: int | None = None
    ev_total_energy_wh: float | None = None
    ev_plug_and_charge: bool = False
    ev_plug_and_charge_auto_start: int | None = None
    ev_plug_and_charge_auto_start_enum: PlugAndChargeAutoStart | None = None
    ev_charger_setpoint_raw: int | None = None
    ev_max_grid_power_draw_raw: int | None = None
    ev_soc_pct: float | None = None
    ev_soc_pct_updated_at: float | None = None

    # Computed diagnostics
    l1_voltage_drop_pct: float | None = None
    l2_voltage_drop_pct: float | None = None
    l3_voltage_drop_pct: float | None = None

    # Control output
    commanded_setpoint_w: float | None = None

    # Configuration (loaded from config, updated via MQTT)
    charge_mode: str = "Eco"
    manual_power_w: float = 3680.0
    ev_min_soc_pct: float = 40.0
    ev_max_soc_pct: float = 80.0
    ev_battery_capacity_kwh: float = 82.0
    solar_battery_discharge_floor_pct: float = 20.0
    solar_battery_discharge_start: str = "23:00"
    solar_battery_discharge_end: str = "06:00"
    solar_battery_max_ev_charge_power_w: float = 5000.0
    solar_battery_max_discharge_w: float = 6000.0
    control_loop_interval_s: float = 10.0
    eco_mean_window_minutes: int = 5
    solar_battery_day_power_limit_w: float = -1500.0
    eco_day_min_battery_soc_pct: float = 75.0
    eco_day_battery_full_pct: float = 96.0
    eco_day_battery_full_exit_pct: float = 90.0
    eco_day_ramp_step_w: float = 200.0
    correction_pct: float = 5.6

    # Device connection config
    ev_charger_ip: str = ""
    ev_charger_port: int = 502
    victron_ip: str = ""
    victron_port: int = 502
    victron_grid_meter_unit_id: int = 30

    # MQTT broker config (bootstrap only, not updated via MQTT)
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""


PERSISTED_FIELDS: set[str] = {
    "charge_mode",
    "manual_power_w",
    "ev_min_soc_pct",
    "ev_max_soc_pct",
    "ev_battery_capacity_kwh",
    "solar_battery_discharge_floor_pct",
    "solar_battery_discharge_start",
    "solar_battery_discharge_end",
    "solar_battery_max_ev_charge_power_w",
    "solar_battery_max_discharge_w",
    "control_loop_interval_s",
    "eco_mean_window_minutes",
    "solar_battery_day_power_limit_w",
    "eco_day_min_battery_soc_pct",
    "eco_day_battery_full_pct",
    "eco_day_battery_full_exit_pct",
    "eco_day_ramp_step_w",
    "correction_pct",
    "ev_charger_ip",
    "ev_charger_port",
    "victron_ip",
    "victron_port",
    "victron_grid_meter_unit_id",
    "mqtt_host",
    "mqtt_port",
    "mqtt_username",
    "mqtt_password",
}


@dataclass(frozen=True)
class StateSnapshot:
    """Immutable snapshot enqueued by the control loop for MQTT publishing."""

    ev_connected: bool = False
    ev_charger_status: int | None = None
    ev_charger_status_display: str | None = None
    ev_comm_connection_status_raw: int | None = None
    ev_comm_wifi_router_connected: bool | None = None
    ev_comm_iot_cloud_connected: bool | None = None
    ev_comm_inverter_online: bool | None = None
    ev_comm_mid_meter_online: bool | None = None
    ev_comm_gw_meter_online: bool | None = None
    ev_comm_ems_online: bool | None = None
    ev_serial_number: str | None = None
    ev_advanced_charging_mode_display: str | None = None
    ev_plug_and_charge_auto_start_display: str | None = None
    ev_single_phase_switching_display: str | None = None
    ev_max_grid_power_draw_w: float | None = None
    ev_active_power_w: float | None = None
    ev_session_energy_wh: float | None = None
    ev_voltage_l1_v: float | None = None
    ev_voltage_l2_v: float | None = None
    ev_voltage_l3_v: float | None = None
    ev_current_a: float | None = None
    ev_current_b: float | None = None
    ev_current_c: float | None = None
    ev_completion_time_h: int | None = None
    ev_total_energy_wh: float | None = None
    ev_soc_pct: float | None = None
    l1_voltage_drop_pct: float | None = None
    l2_voltage_drop_pct: float | None = None
    l3_voltage_drop_pct: float | None = None
    commanded_setpoint_w: float | None = None
    uptime_s: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
