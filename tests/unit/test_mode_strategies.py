"""Direct unit tests for charging mode strategies."""

from __future__ import annotations

import time as _time

from app.control.constants import _MAX_CHARGE_W, _MIN_CHARGE_W
from app.control.mode_strategies import (
    EcoNightModeHandler,
    ManualModeHandler,
    StandbyModeHandler,
    compute_setpoint,
    resolve_mode_handler,
)
from app.state import AppState, ChargeModeState
from tests.unit.helpers import make_ns_loop


class TestResolveModeHandler:
    def test_no_vehicle_returns_no_vehicle_handler(self):
        loop = make_ns_loop(AppState(ev_connected=False, charge_mode="Eco"))

        assert resolve_mode_handler(loop).name == "no_vehicle"

    def test_unhealthy_ev_comms_returns_no_vehicle_handler(self):
        loop = make_ns_loop(AppState(ev_connected=True, ev_comm_healthy=False, charge_mode="Eco"))

        assert resolve_mode_handler(loop).name == "no_vehicle"

    def test_standby_returns_standby_handler(self):
        loop = make_ns_loop(AppState(ev_connected=True, charge_mode="Standby"))

        assert resolve_mode_handler(loop).name == "standby"

    def test_victron_down_returns_dedicated_handler(self):
        loop = make_ns_loop(AppState(ev_connected=True, charge_mode="Eco"), victron_connected=False)

        assert resolve_mode_handler(loop).name == "eco_victron_down"


class TestManualModeHandler:
    def test_clamps_power_and_sets_mode_state(self):
        loop = make_ns_loop(AppState(ev_connected=True, charge_mode="Manual", manual_power_w=50_000.0))

        result = ManualModeHandler().compute(loop)

        assert result == _MAX_CHARGE_W
        assert loop._state_machine.mode_state == ChargeModeState.MANUAL


class TestStandbyModeHandler:
    def test_resets_eco_state_and_returns_zero(self):
        loop = make_ns_loop(
            AppState(ev_connected=True, charge_mode="Standby"),
            _eco_charging=True,
            _eco_day_setpoint_w=7000.0,
        )

        result = StandbyModeHandler().compute(loop)

        assert result == 0.0
        assert loop._eco_charging is False
        assert loop._eco_day_setpoint_w == _MIN_CHARGE_W
        assert loop._state_machine.mode_state == ChargeModeState.STANDBY


class TestEcoNightModeHandler:
    def test_battery_flat_uses_grid_fallback_mode_state(self):
        state = AppState(
            ev_connected=True,
            charge_mode="Eco",
            solar_battery_soc_pct=20.0,
            solar_battery_power_w=200.0,
            solar_battery_discharge_floor_pct=20.0,
            ev_min_soc_pct=40.0,
            ev_soc_pct=20.0,
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        loop = make_ns_loop(state)

        result = EcoNightModeHandler().compute(loop)

        assert result >= _MIN_CHARGE_W
        assert loop._state_machine.mode_state == ChargeModeState.ECO_NIGHT_GRID_FALLBACK


class TestComputeSetpoint:
    def test_dispatches_through_resolved_handler(self):
        loop = make_ns_loop(AppState(ev_connected=True, charge_mode="Manual", manual_power_w=4200.0))

        assert compute_setpoint(loop) == _MIN_CHARGE_W
