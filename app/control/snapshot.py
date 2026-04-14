"""Snapshot-building helpers for the control loop."""

from __future__ import annotations

import time as _time
from datetime import datetime
from typing import TYPE_CHECKING

from app.state import StateSnapshot

if TYPE_CHECKING:
    from app.control.loop import ControlLoop


def build_snapshot(loop: ControlLoop) -> StateSnapshot:
    """Build an immutable snapshot for MQTT publication."""
    return StateSnapshot(
        ev_connected=loop._state.ev_connected,
        ev_charger_status=loop._state.ev_charger_status,
        ev_charger_status_display=loop._state.ev_charger_status_enum.display_name
        if loop._state.ev_charger_status_enum is not None
        else None,
        ev_comm_connection_status_raw=loop._state.ev_comm_connection_status_raw,
        ev_comm_wifi_router_connected=loop._state.ev_comm_wifi_router_connected,
        ev_comm_iot_cloud_connected=loop._state.ev_comm_iot_cloud_connected,
        ev_comm_inverter_online=loop._state.ev_comm_inverter_online,
        ev_comm_mid_meter_online=loop._state.ev_comm_mid_meter_online,
        ev_comm_gw_meter_online=loop._state.ev_comm_gw_meter_online,
        ev_comm_ems_online=loop._state.ev_comm_ems_online,
        ev_serial_number=loop._state.ev_serial_number,
        ev_advanced_charging_mode_display=loop._state.ev_advanced_charging_mode_enum.display_name
        if loop._state.ev_advanced_charging_mode_enum is not None
        else None,
        ev_plug_and_charge_auto_start_display=loop._state.ev_plug_and_charge_auto_start_enum.display_name
        if loop._state.ev_plug_and_charge_auto_start_enum is not None
        else None,
        ev_single_phase_switching_display=loop._state.ev_single_phase_switching_enum.display_name
        if loop._state.ev_single_phase_switching_enum is not None
        else None,
        ev_max_grid_power_draw_w=(loop._state.ev_max_grid_power_draw_raw * 100.0)
        if loop._state.ev_max_grid_power_draw_raw is not None
        else None,
        ev_active_power_w=loop._state.ev_active_power_w,
        ev_session_energy_wh=loop._state.ev_session_energy_wh,
        ev_voltage_l1_v=loop._state.ev_voltage_l1_v,
        ev_voltage_l2_v=loop._state.ev_voltage_l2_v,
        ev_voltage_l3_v=loop._state.ev_voltage_l3_v,
        ev_current_a=loop._state.ev_current_a,
        ev_current_b=loop._state.ev_current_b,
        ev_current_c=loop._state.ev_current_c,
        ev_completion_time_h=loop._state.ev_completion_time_h,
        ev_total_energy_wh=loop._state.ev_total_energy_wh,
        ev_soc_pct=loop._get_ev_soc(),
        l1_voltage_drop_pct=loop._state.l1_voltage_drop_pct,
        l2_voltage_drop_pct=loop._state.l2_voltage_drop_pct,
        l3_voltage_drop_pct=loop._state.l3_voltage_drop_pct,
        commanded_setpoint_w=loop._state.commanded_setpoint_w,
        uptime_s=round(_time.monotonic() - loop._start_time),
        timestamp=datetime.now(),  # noqa: DTZ005
    )
