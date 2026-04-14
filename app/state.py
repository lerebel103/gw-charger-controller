"""Core data models for the EV charger integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum


class ChargerStatus(IntEnum):
    """GW22K-HCA-20 charger status codes (register 10017).

    These status values indicate the operational mode of the EV charger.
    """

    IDLE_NO_CONNECTOR = 0
    IDLE_CONNECTOR_PLUGGED = 1
    HANDSHAKING_WITH_VEHICLE = 2
    CHARGING_IN_PROGRESS = 3
    CHARGING_COMPLETED = 4
    ABNORMAL_ALARM = 5
    SCHEDULED_START = 6
    MAINTENANCE = 7
    START_FAILED = 8
    SYSTEM_UPGRADE_IN_PROGRESS = 9
    CHARGING_INTERRUPTED_INSUFFICIENT_PV_BATTERY = 10
    UNKNOWN = 255

    @classmethod
    def from_register(cls, value: int | None) -> ChargerStatus | None:
        """Convert register value to ChargerStatus enum.

        Returns None if value is None, or the appropriate enum value.
        For unknown values, returns Unknown status.
        """
        if value is None:
            return None
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def display_name(self) -> str:
        """Human-readable name for the status."""
        names = {
            0: "Idle (no connector plugged)",
            1: "Idle (connector plugged)",
            2: "Handshaking with vehicle",
            3: "Charging in progress",
            4: "Charging completed",
            5: "Abnormal alarm",
            6: "Scheduled start",
            7: "Maintenance",
            8: "Start failed",
            9: "System upgrade in progress",
            10: "Charging interrupted (insufficient PV/battery power)",
            255: "Unknown",
        }
        return names.get(self.value, f"Unknown ({self.value})")

    @classmethod
    def ha_options(cls) -> list[str]:
        """Return Home Assistant enum options in stable declaration order."""
        return [status.display_name for status in cls]


class AdvancedChargingMode(IntEnum):
    """GW22K-HCA-20 advanced charging mode (register 10032)."""

    FAST_CHARGING = 0
    PV_CHARGING = 1
    PV_BATTERY_HYBRID_CHARGING = 2
    UNKNOWN = 255

    @classmethod
    def from_register(cls, value: int | None) -> AdvancedChargingMode | None:
        """Convert register value to AdvancedChargingMode enum."""
        if value is None:
            return None
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def display_name(self) -> str:
        names = {
            0: "Fast charging",
            1: "PV charging",
            2: "PV + battery hybrid charging",
            255: "Unknown",
        }
        return names.get(self.value, f"Unknown ({self.value})")

    @classmethod
    def ha_options(cls) -> list[str]:
        """Return Home Assistant enum options in stable declaration order."""
        return [mode.display_name for mode in cls if mode != cls.UNKNOWN]

    @classmethod
    def from_display_name(cls, value: str) -> AdvancedChargingMode | None:
        """Map Home Assistant display label to enum value."""
        for mode in cls:
            if mode.display_name == value:
                return mode
        return None


class PlugAndChargeAutoStart(IntEnum):
    """GW22K-HCA-20 plug-and-charge auto start (register 10019)."""

    OFF = 0
    ON = 1
    UNKNOWN = 255

    @classmethod
    def from_register(cls, value: int | None) -> PlugAndChargeAutoStart | None:
        """Convert register value to PlugAndChargeAutoStart enum."""
        if value is None:
            return None
        # Some charger firmware reports ON as 2 instead of 1.
        if value == 2:
            return cls.ON
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def display_name(self) -> str:
        names = {
            0: "Off",
            1: "On",
            255: "Unknown",
        }
        return names.get(self.value, f"Unknown ({self.value})")

    @classmethod
    def ha_options(cls) -> list[str]:
        """Return Home Assistant enum options in stable declaration order."""
        return [value.display_name for value in cls if value != cls.UNKNOWN]

    @classmethod
    def from_display_name(cls, value: str) -> PlugAndChargeAutoStart | None:
        """Map Home Assistant display label to enum value."""
        for item in cls:
            if item.display_name == value:
                return item
        return None


class SinglePhaseSwitching(IntEnum):
    """GW22K-HCA-20 single phase switching (register 10023)."""

    DISABLED = 0
    ENABLED = 1
    UNKNOWN = 255

    @classmethod
    def from_register(cls, value: int | None) -> SinglePhaseSwitching | None:
        """Convert register value to SinglePhaseSwitching enum."""
        if value is None:
            return None
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def display_name(self) -> str:
        names = {
            0: "Disabled",
            1: "Enabled",
            255: "Unknown",
        }
        return names.get(self.value, f"Unknown ({self.value})")

    @classmethod
    def from_switch_payload(cls, value: str) -> SinglePhaseSwitching | None:
        """Map Home Assistant switch payload to enum value."""
        if value == "ON":
            return cls.ENABLED
        if value == "OFF":
            return cls.DISABLED
        return None


@dataclass
class AppState:
    """Central in-memory state. Single-threaded asyncio — no locking needed."""

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
    ev_charger_status_enum: ChargerStatus | None = None  # decoded status for internal use
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
    ev_plug_and_charge: bool = False  # compatibility mirror of register 10019 (True when value == 1)
    ev_plug_and_charge_auto_start: int | None = None
    ev_plug_and_charge_auto_start_enum: PlugAndChargeAutoStart | None = None
    ev_charger_setpoint_raw: int | None = None  # register 10029: current setpoint as read from charger
    ev_soc_pct: float | None = None
    ev_soc_pct_updated_at: float | None = None  # time.monotonic() of last SOC update

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
    ev_max_soc_pct: float = 80.0  # max EV charge SOC; resets to 80% on disconnect
    ev_battery_capacity_kwh: float = 82.0  # EV battery capacity in kWh
    solar_battery_discharge_floor_pct: float = 20.0
    solar_battery_discharge_start: str = "23:00"
    solar_battery_discharge_end: str = "06:00"
    solar_battery_max_ev_charge_power_w: float = 5000.0
    solar_battery_max_discharge_w: float = 6000.0
    control_loop_interval_s: float = 10.0
    eco_mean_window_minutes: int = 5  # rolling average window for eco start/stop decisions (1–10 min)
    # stop EV charging when mean battery power drops below this (W, negative = discharging)
    solar_battery_day_power_limit_w: float = -1500.0
    eco_day_min_battery_soc_pct: float = 90.0  # don't start eco day EV charging until battery SOC is above this %
    correction_pct: float = 5.6  # correction applied to EV current and active power readings

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


# Fields persisted to config YAML (excludes runtime readings and computed values)
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
    ev_charger_status_display: str | None = None  # human-readable status name
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
    ev_max_charging_power_w: float | None = None
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
