"""Snapshot-building helpers for the control loop."""

import time as _time
from datetime import datetime

from app.control.power_utils import get_ev_soc
from app.control.protocols import SnapshotLoopProtocol
from app.state import StateSnapshot


def build_snapshot(loop: SnapshotLoopProtocol) -> StateSnapshot:
    """Build an immutable snapshot for MQTT publication.

    When EV communications are unhealthy (``ev_comm_healthy is False``), every
    EV-charger-derived field is masked to ``None`` so that ``_pub_if`` skips the
    publish and ``expire_after`` can elapse, flipping those entities to
    ``unknown`` in Home Assistant instead of pinning them to stale values.
    Victron-derived fields, the SOC (already staleness-guarded by
    ``get_ev_soc``), the breaker-headroom diagnostics (Victron + config
    derived), the serial number (discovery identity), and local control values
    are intentionally left unmasked.
    """
    ev_ok = loop._state.ev_comm_healthy
    return StateSnapshot(
        ev_connected=loop._state.ev_connected if ev_ok else None,
        ev_charger_status=loop._state.ev_charger_status if ev_ok else None,
        ev_charger_status_display=loop._state.ev_charger_status_enum.display_name
        if ev_ok and loop._state.ev_charger_status_enum is not None
        else None,
        ev_comm_connection_status_raw=loop._state.ev_comm_connection_status_raw if ev_ok else None,
        ev_comm_wifi_router_connected=loop._state.ev_comm_wifi_router_connected if ev_ok else None,
        ev_comm_iot_cloud_connected=loop._state.ev_comm_iot_cloud_connected if ev_ok else None,
        ev_comm_inverter_online=loop._state.ev_comm_inverter_online if ev_ok else None,
        ev_comm_mid_meter_online=loop._state.ev_comm_mid_meter_online if ev_ok else None,
        ev_comm_gw_meter_online=loop._state.ev_comm_gw_meter_online if ev_ok else None,
        ev_comm_ems_online=loop._state.ev_comm_ems_online if ev_ok else None,
        ev_serial_number=loop._state.ev_serial_number,
        ev_advanced_charging_mode_display=loop._state.ev_advanced_charging_mode_enum.display_name
        if ev_ok and loop._state.ev_advanced_charging_mode_enum is not None
        else None,
        ev_plug_and_charge_auto_start_display=loop._state.ev_plug_and_charge_auto_start_enum.display_name
        if ev_ok and loop._state.ev_plug_and_charge_auto_start_enum is not None
        else None,
        ev_single_phase_switching_display=loop._state.ev_single_phase_switching_enum.display_name
        if ev_ok and loop._state.ev_single_phase_switching_enum is not None
        else None,
        ev_active_power_w=loop._state.ev_active_power_w if ev_ok else None,
        ev_session_energy_wh=loop._state.ev_session_energy_wh if ev_ok else None,
        ev_voltage_l1_v=loop._state.ev_voltage_l1_v if ev_ok else None,
        ev_voltage_l2_v=loop._state.ev_voltage_l2_v if ev_ok else None,
        ev_voltage_l3_v=loop._state.ev_voltage_l3_v if ev_ok else None,
        ev_current_a=loop._state.ev_current_a if ev_ok else None,
        ev_current_b=loop._state.ev_current_b if ev_ok else None,
        ev_current_c=loop._state.ev_current_c if ev_ok else None,
        ev_completion_time_h=loop._state.ev_completion_time_h if ev_ok else None,
        ev_total_energy_wh=loop._state.ev_total_energy_wh if ev_ok else None,
        ev_soc_pct=get_ev_soc(loop),
        l1_voltage_drop_pct=loop._state.l1_voltage_drop_pct if ev_ok else None,
        l2_voltage_drop_pct=loop._state.l2_voltage_drop_pct if ev_ok else None,
        l3_voltage_drop_pct=loop._state.l3_voltage_drop_pct if ev_ok else None,
        l1_breaker_headroom_pct=loop._state.l1_breaker_headroom_pct,
        l2_breaker_headroom_pct=loop._state.l2_breaker_headroom_pct,
        l3_breaker_headroom_pct=loop._state.l3_breaker_headroom_pct,
        victron_l1_current_a=loop._state.victron_l1_current_a,
        victron_l2_current_a=loop._state.victron_l2_current_a,
        victron_l3_current_a=loop._state.victron_l3_current_a,
        commanded_setpoint_w=loop._state.commanded_setpoint_w,
        uptime_s=round(_time.monotonic() - loop._start_time),
        timestamp=datetime.now(),  # noqa: DTZ005
    )
