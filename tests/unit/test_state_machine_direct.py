"""Direct unit tests for ChargingStateMachine."""

from __future__ import annotations

import time as _time

from app.control.state_machine import ChargingStateMachine
from app.state import AppState, ChargerStatus, ChargeSessionState
from tests.unit.helpers import make_ns_loop


class TestSessionState:
    def test_set_session_state_updates_field(self):
        loop = make_ns_loop(AppState())
        machine = ChargingStateMachine(loop)

        machine.set_session_state(ChargeSessionState.CHARGING)

        assert loop._charging_session_state == ChargeSessionState.CHARGING


class TestStopReason:
    def test_standby_reason(self):
        loop = make_ns_loop(AppState(ev_connected=True, charge_mode="Standby"))
        machine = ChargingStateMachine(loop)

        assert machine.determine_stop_reason() == "standby"

    def test_eco_day_battery_reason(self):
        # Use a narrow discharge window guaranteed to not contain the current time
        # by placing it exactly 12 hours from now (±30 min)
        from datetime import datetime, timedelta

        now = datetime.now()
        window_start = (now + timedelta(hours=12)).strftime("%H:%M")
        window_end = (now + timedelta(hours=13)).strftime("%H:%M")

        state = AppState(
            ev_connected=True,
            charge_mode="Eco",
            solar_battery_discharge_start=window_start,
            solar_battery_discharge_end=window_end,
            solar_battery_soc_pct=95.0,
            solar_battery_day_power_limit_w=-1500.0,
        )
        loop = make_ns_loop(state, _battery_power_samples=[(_time.monotonic(), -2000.0)])
        machine = ChargingStateMachine(loop)

        assert machine.determine_stop_reason() == "eco_day_mean_battery"


class TestCommands:
    def test_should_send_start_command_for_idle_plugged(self):
        state = AppState(
            ev_connected=True,
            ev_charger_status_enum=ChargerStatus.IDLE_CONNECTOR_PLUGGED,
        )
        loop = make_ns_loop(state)
        machine = ChargingStateMachine(loop)

        assert machine.should_send_start_command(5600.0) is True

    def test_should_send_stop_command_when_active_and_zero_setpoint(self):
        state = AppState(
            ev_connected=True,
            ev_charger_status_enum=ChargerStatus.CHARGING_IN_PROGRESS,
        )
        loop = make_ns_loop(state)
        machine = ChargingStateMachine(loop)

        assert machine.should_send_stop_command(0.0) is True


class TestApplyChargingEvents:
    def test_idle_positive_setpoint_emits_started_event(self):
        loop = make_ns_loop(AppState(ev_connected=True, charge_mode="Manual"))
        machine = ChargingStateMachine(loop)

        result = machine.apply_charging_events(4200.0)

        assert result == 4200.0
        assert loop._charging_session_state == ChargeSessionState.CHARGING
        event = loop._publish_queue.get_nowait()
        assert event["event"] == "started"

    def test_stopped_pending_zero_setpoint_emits_stopped_event(self):
        state = AppState(
            ev_connected=True,
            charge_mode="Eco",
            ev_session_energy_wh=1234.0,
            ev_soc_pct=55.0,
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        loop = make_ns_loop(
            state,
            _charging_session_state=ChargeSessionState.STOPPED_PENDING,
            _stopped_at=_time.monotonic() - 30.0,
            _stopping_reason="standby",
        )
        machine = ChargingStateMachine(loop)

        result = machine.apply_charging_events(0.0)

        assert result == 0.0
        assert loop._charging_session_state == ChargeSessionState.IDLE
        event = loop._publish_queue.get_nowait()
        assert event["event"] == "stopped"
        assert event["reason"] == "standby"
