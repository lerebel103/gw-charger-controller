"""Unit tests for build_snapshot EV-comms masking.

When EV communications are unhealthy, every EV-charger-derived field must be
masked to ``None`` so that ``_pub_if`` skips the publish and ``expire_after``
can elapse. Victron-derived fields, the staleness-guarded SOC, the breaker
headroom diagnostics, the serial number, and local control values must survive.
"""

from __future__ import annotations

import time
import types

from app.control.snapshot import build_snapshot
from app.state import (
    AdvancedChargingMode,
    AppState,
    ChargerStatus,
    PlugAndChargeAutoStart,
    SinglePhaseSwitching,
)


def _loop(state: AppState) -> types.SimpleNamespace:
    """Minimal SnapshotLoopProtocol stub (``_state`` + ``_start_time``)."""
    return types.SimpleNamespace(_state=state, _start_time=0.0)


def _populate_ev_fields(state: AppState) -> None:
    """Fill AppState with representative, non-None EV-derived readings."""
    state.ev_connected = True
    state.ev_charger_status = 3
    state.ev_charger_status_enum = ChargerStatus.CHARGING_IN_PROGRESS
    state.ev_comm_connection_status_raw = 1
    state.ev_comm_wifi_router_connected = True
    state.ev_comm_iot_cloud_connected = True
    state.ev_comm_inverter_online = True
    state.ev_comm_mid_meter_online = True
    state.ev_comm_gw_meter_online = True
    state.ev_comm_ems_online = True
    state.ev_advanced_charging_mode_enum = AdvancedChargingMode.PV_CHARGING
    state.ev_plug_and_charge_auto_start_enum = PlugAndChargeAutoStart.ON
    state.ev_single_phase_switching_enum = SinglePhaseSwitching.ENABLED
    state.ev_active_power_w = 1500.0
    state.ev_session_energy_wh = 1234.0
    state.ev_voltage_l1_v = 230.0
    state.ev_voltage_l2_v = 231.0
    state.ev_voltage_l3_v = 229.0
    state.ev_current_a = 6.0
    state.ev_current_b = 6.1
    state.ev_current_c = 5.9
    state.ev_completion_time_h = 3
    state.ev_total_energy_wh = 999999.0
    state.l1_voltage_drop_pct = 1.2
    state.l2_voltage_drop_pct = 1.3
    state.l3_voltage_drop_pct = 1.4


# Fields that must become None when EV comms are unhealthy.
_MASKED_SNAPSHOT_FIELDS = [
    "ev_connected",
    "ev_charger_status",
    "ev_charger_status_display",
    "ev_comm_connection_status_raw",
    "ev_comm_wifi_router_connected",
    "ev_comm_iot_cloud_connected",
    "ev_comm_inverter_online",
    "ev_comm_mid_meter_online",
    "ev_comm_gw_meter_online",
    "ev_comm_ems_online",
    "ev_advanced_charging_mode_display",
    "ev_plug_and_charge_auto_start_display",
    "ev_single_phase_switching_display",
    "ev_active_power_w",
    "ev_session_energy_wh",
    "ev_voltage_l1_v",
    "ev_voltage_l2_v",
    "ev_voltage_l3_v",
    "ev_current_a",
    "ev_current_b",
    "ev_current_c",
    "ev_completion_time_h",
    "ev_total_energy_wh",
    "l1_voltage_drop_pct",
    "l2_voltage_drop_pct",
    "l3_voltage_drop_pct",
]


class TestBuildSnapshotEVCommsUnhealthy:
    def test_stale_ev_fields_are_masked_to_none(self):
        state = AppState()
        _populate_ev_fields(state)
        # Comms failed: EV read left stale values populated but flipped the flag.
        state.ev_comm_healthy = False

        snapshot = build_snapshot(_loop(state))

        for name in _MASKED_SNAPSHOT_FIELDS:
            assert getattr(snapshot, name) is None, f"{name} should be masked to None"
        assert snapshot.ev_connected is None

    def test_non_ev_fields_survive_an_ev_outage(self):
        state = AppState()
        _populate_ev_fields(state)
        state.ev_comm_healthy = False

        # Discovery identity must survive the outage.
        state.ev_serial_number = "SN-12345"
        # Victron-derived currents come from a separate client.
        state.victron_l1_current_a = 5.0
        state.victron_l2_current_a = 5.1
        state.victron_l3_current_a = 4.9
        # Breaker headroom is Victron + config derived, recomputed every cycle.
        state.l1_breaker_headroom_pct = 50.0
        state.l2_breaker_headroom_pct = 60.0
        state.l3_breaker_headroom_pct = 70.0
        # SOC is staleness-guarded by get_ev_soc(), independent of comm health.
        state.ev_soc_pct = 42.0
        state.ev_soc_pct_updated_at = time.monotonic()
        # Local control value.
        state.commanded_setpoint_w = 1500.0

        snapshot = build_snapshot(_loop(state))

        assert snapshot.ev_serial_number == "SN-12345"
        assert snapshot.victron_l1_current_a == 5.0
        assert snapshot.victron_l2_current_a == 5.1
        assert snapshot.victron_l3_current_a == 4.9
        assert snapshot.l1_breaker_headroom_pct == 50.0
        assert snapshot.l2_breaker_headroom_pct == 60.0
        assert snapshot.l3_breaker_headroom_pct == 70.0
        assert snapshot.ev_soc_pct == 42.0
        assert snapshot.commanded_setpoint_w == 1500.0


class TestBuildSnapshotEVCommsHealthy:
    def test_happy_path_passes_ev_fields_through(self):
        state = AppState()
        _populate_ev_fields(state)
        state.ev_comm_healthy = True
        state.ev_soc_pct = 42.0
        state.ev_soc_pct_updated_at = time.monotonic()

        snapshot = build_snapshot(_loop(state))

        assert snapshot.ev_connected is True
        assert snapshot.ev_active_power_w == 1500.0
        assert snapshot.ev_voltage_l1_v == 230.0
        assert snapshot.ev_current_a == 6.0
        assert snapshot.l1_voltage_drop_pct == 1.2
        assert snapshot.ev_charger_status == 3
        assert snapshot.ev_charger_status_display == ChargerStatus.CHARGING_IN_PROGRESS.display_name
        assert snapshot.ev_comm_wifi_router_connected is True
        assert snapshot.ev_soc_pct == 42.0
