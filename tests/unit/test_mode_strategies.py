"""Direct unit tests for charging mode strategies."""

from __future__ import annotations

import time as _time

import pytest

from app.control.constants import _MAX_CHARGE_W, _MIN_CHARGE_W
from app.control.mode_strategies import (
    EcoDayModeHandler,
    EcoNightModeHandler,
    ManualModeHandler,
    StandbyModeHandler,
    compute_setpoint,
    resolve_mode_handler,
)
from app.state import AppState, ChargeModeState
from tests.unit.helpers import fill_battery_samples, fill_grid_samples, make_control_loop, make_ns_loop


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

    def test_standby_precedence_when_ev_comms_unhealthy(self):
        loop = make_ns_loop(AppState(ev_connected=True, ev_comm_healthy=False, charge_mode="Standby"))

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
            solar_battery_discharge_start="20:00",
            solar_battery_discharge_end="12:00",
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


class TestEcoDayMinCharge:
    def test_holds_floor_when_enabled_and_soc_below_threshold_with_no_solar_data(self):
        """Renamed from test_returns_min_charge_when_enabled_and_soc_below_threshold.

        Min charge is now a floor, not an unconditional early exit. With no rolling grid/battery data the eco start
        conditions are not met, so the handler holds the floor: it still returns 4400 W (not 0) in ECO_DAY_MIN_CHARGE.
        The assertions are unchanged; only the reason differs (hold-floor path instead of early return).
        """
        state = AppState(
            ev_connected=True,
            charge_mode="Eco",
            eco_day_min_charge_enabled=True,
            ev_min_soc_pct=50.0,
            ev_soc_pct=30.0,
            solar_battery_soc_pct=95.0,
            solar_battery_discharge_start="23:00",
            solar_battery_discharge_end="06:00",
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        loop = make_ns_loop(state)

        from app.control.mode_strategies import EcoDayModeHandler

        result = EcoDayModeHandler().compute(loop)

        assert result == _MIN_CHARGE_W
        assert loop._state_machine.mode_state == ChargeModeState.ECO_DAY_MIN_CHARGE

    def test_does_not_trigger_when_disabled(self):
        state = AppState(
            ev_connected=True,
            charge_mode="Eco",
            eco_day_min_charge_enabled=False,
            ev_min_soc_pct=50.0,
            ev_soc_pct=30.0,
            solar_battery_soc_pct=95.0,
            solar_battery_discharge_start="23:00",
            solar_battery_discharge_end="06:00",
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        loop = make_ns_loop(state)

        from app.control.mode_strategies import EcoDayModeHandler

        EcoDayModeHandler().compute(loop)

        # Should fall through to normal eco day logic, not min charge
        assert loop._state_machine.mode_state != ChargeModeState.ECO_DAY_MIN_CHARGE

    def test_does_not_trigger_when_soc_is_none(self):
        state = AppState(
            ev_connected=True,
            charge_mode="Eco",
            eco_day_min_charge_enabled=True,
            ev_min_soc_pct=50.0,
            ev_soc_pct=None,
            solar_battery_soc_pct=95.0,
            solar_battery_discharge_start="23:00",
            solar_battery_discharge_end="06:00",
        )
        loop = make_ns_loop(state)

        from app.control.mode_strategies import EcoDayModeHandler

        EcoDayModeHandler().compute(loop)

        assert loop._state_machine.mode_state != ChargeModeState.ECO_DAY_MIN_CHARGE

    def test_does_not_trigger_when_soc_at_threshold(self):
        state = AppState(
            ev_connected=True,
            charge_mode="Eco",
            eco_day_min_charge_enabled=True,
            ev_min_soc_pct=50.0,
            ev_soc_pct=50.0,
            solar_battery_soc_pct=95.0,
            solar_battery_discharge_start="23:00",
            solar_battery_discharge_end="06:00",
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        loop = make_ns_loop(state)

        from app.control.mode_strategies import EcoDayModeHandler

        EcoDayModeHandler().compute(loop)

        assert loop._state_machine.mode_state != ChargeModeState.ECO_DAY_MIN_CHARGE

    def test_does_not_trigger_when_soc_above_threshold(self):
        state = AppState(
            ev_connected=True,
            charge_mode="Eco",
            eco_day_min_charge_enabled=True,
            ev_min_soc_pct=50.0,
            ev_soc_pct=80.0,
            solar_battery_soc_pct=95.0,
            solar_battery_discharge_start="23:00",
            solar_battery_discharge_end="06:00",
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        loop = make_ns_loop(state)

        from app.control.mode_strategies import EcoDayModeHandler

        EcoDayModeHandler().compute(loop)

        assert loop._state_machine.mode_state != ChargeModeState.ECO_DAY_MIN_CHARGE


def _floor_loop(*, ev_soc=30.0, home_soc=100.0, battery_w=2000.0, mean_grid=-1500.0, mean_battery=2000.0, **overrides):
    """Real ControlLoop (real state machine) in Eco day, min charge ON, EV SOC below the 50 % minimum."""
    params = {"eco_day_min_charge_enabled": True, **overrides}
    state = AppState(
        ev_connected=True,
        charge_mode="Eco",
        ev_min_soc_pct=50.0,
        ev_soc_pct=ev_soc,
        solar_battery_soc_pct=home_soc,
        solar_battery_power_w=battery_w,
        eco_day_ramp_step_w=40.0,
        **params,
    )
    state.ev_soc_pct_updated_at = _time.monotonic()
    cl = make_control_loop(state)
    cl._eco_charging = True
    cl._eco_day_setpoint_w = _MIN_CHARGE_W
    fill_grid_samples(cl, mean_grid)
    fill_battery_samples(cl, mean_battery)
    return cl, state


class TestEcoDayMinChargeIsFloor:
    """Min charge is a floor: >= 4400 W while EV SOC < ev_min_soc_pct, the normal ramp runs above it."""

    def test_ramps_above_floor_with_surplus(self):
        cl, _ = _floor_loop()
        assert EcoDayModeHandler().compute(cl) == _MIN_CHARGE_W + 40.0
        assert cl._charge_mode_state == ChargeModeState.ECO_DAY_RAMPING

    def test_no_solar_holds_floor_not_zero(self):
        cl, _ = _floor_loop(home_soc=70.0, mean_grid=500.0, mean_battery=-500.0)  # below the 75 % home-battery gate
        cl._eco_day_setpoint_w = 6000.0
        assert EcoDayModeHandler().compute(cl) == _MIN_CHARGE_W
        assert cl._charge_mode_state == ChargeModeState.ECO_DAY_MIN_CHARGE
        assert cl._eco_charging is False
        assert cl._eco_day_setpoint_w == _MIN_CHARGE_W

    def test_cooldown_does_not_block_floor(self):
        cl, _ = _floor_loop()
        cl._eco_charging = False
        cl._eco_day_stopped_at = _time.monotonic() - 60
        assert EcoDayModeHandler().compute(cl) >= _MIN_CHARGE_W
        assert cl._eco_charging is True

    def test_battery_stop_holds_floor_without_cooldown(self):
        cl, _ = _floor_loop(mean_battery=-4000.0, battery_w=-4000.0)
        assert EcoDayModeHandler().compute(cl) == _MIN_CHARGE_W
        assert cl._eco_charging is False
        assert cl._eco_day_stopped_at is None  # no 300 s zero-watt pause when the SOC later crosses the minimum

    def test_not_full_battery_holds_floor(self):
        cl, _ = _floor_loop(home_soc=93.0)
        assert EcoDayModeHandler().compute(cl) == _MIN_CHARGE_W
        assert cl._charge_mode_state == ChargeModeState.ECO_DAY_MIN_CHARGE

    def test_ramps_down_to_floor_not_below(self):
        cl, _ = _floor_loop(battery_w=-300.0)
        cl._eco_day_setpoint_w = 6000.0
        assert EcoDayModeHandler().compute(cl) == 6000.0 - 40.0  # one ramp step down
        cl._eco_day_setpoint_w = _MIN_CHARGE_W
        assert EcoDayModeHandler().compute(cl) == _MIN_CHARGE_W
        assert cl._charge_mode_state == ChargeModeState.ECO_DAY_MIN_CHARGE

    def test_soc_crossing_minimum_does_not_reset_or_drop(self):
        cl, state = _floor_loop(ev_soc=49.9)
        cl._eco_day_setpoint_w = 5000.0
        assert EcoDayModeHandler().compute(cl) == 5040.0
        state.ev_soc_pct = 50.0
        state.ev_soc_pct_updated_at = _time.monotonic()
        assert EcoDayModeHandler().compute(cl) == 5080.0
        assert cl._eco_charging is True

    def test_min_charge_off_still_stops_without_solar(self):
        """Regression guard: without the floor the home-battery SOC gate still returns 0."""
        cl, _ = _floor_loop(home_soc=70.0, eco_day_min_charge_enabled=False)
        assert EcoDayModeHandler().compute(cl) == 0.0

    @pytest.mark.parametrize("ev_power", [None, 0.0, 4400.0])
    @pytest.mark.parametrize("currents", [None, (6.0, 6.0, 6.0)])
    def test_ramp_ignores_ev_measurements(self, ev_power, currents):
        """The ramp never reads EV power or currents (v0.1.13 follow guard and ev_power<=0 hold removed)."""
        cl, state = _floor_loop(ev_active_power_w=ev_power)
        cl._eco_day_setpoint_w = 5000.0
        if currents is not None:
            state.ev_current_raw_a, state.ev_current_raw_b, state.ev_current_raw_c = currents
            state.ev_current_a, state.ev_current_b, state.ev_current_c = (c * 1.056 for c in currents)
            state.ev_voltage_l1_v = state.ev_voltage_l2_v = state.ev_voltage_l3_v = 240.0
        assert EcoDayModeHandler().compute(cl) == 5040.0

    @pytest.mark.parametrize(
        "battery_w,expected_delta",
        [
            (-150.0, +40.0),
            (-200.0, +40.0),  # boundary: inside the deadband
            (-220.0, -40.0),
            (-5000.0, -40.0),  # ramp-down speed unchanged: one step per cycle
            (None, +40.0),
        ],
    )
    def test_ramp_direction_from_battery_power(self, battery_w, expected_delta):
        cl, _ = _floor_loop(battery_w=battery_w, mean_battery=0.0)
        cl._eco_day_setpoint_w = 9000.0
        assert EcoDayModeHandler().compute(cl) == 9000.0 + expected_delta


def _night_loop(*, min_charge=True, ev_soc=30.0, home_soc=60.0, battery_w=-4000.0, **overrides):
    params = {
        "solar_battery_discharge_floor_pct": 16.0,
        "solar_battery_max_ev_charge_power_w": 4400.0,
        "solar_battery_max_discharge_w": 15000.0,
        "solar_battery_discharge_end": "06:30",
        **overrides,
    }
    state = AppState(
        ev_connected=True,
        charge_mode="Eco",
        eco_day_min_charge_enabled=min_charge,
        ev_min_soc_pct=50.0,
        ev_soc_pct=ev_soc,
        solar_battery_soc_pct=home_soc,
        solar_battery_power_w=battery_w,
        **params,
    )
    state.ev_soc_pct_updated_at = _time.monotonic()
    return make_ns_loop(state), state


class TestEcoNightMinChargeFloor:
    """Switch on and EV SOC < ev_min_soc_pct: Eco night never returns less than 4400 W (min SOC first)."""

    def test_cooldown_holds_floor(self):
        loop, _ = _night_loop()
        loop._eco_night_stopped_at = _time.monotonic() - 10
        assert EcoNightModeHandler().compute(loop) == _MIN_CHARGE_W
        assert loop._state_machine.mode_state == ChargeModeState.ECO_NIGHT_BATTERY

    def test_cooldown_without_floor_still_zero(self):
        for kwargs in ({"min_charge": False}, {"ev_soc": 60.0}):
            loop, _ = _night_loop(**kwargs)
            loop._eco_night_stopped_at = _time.monotonic() - 10
            assert EcoNightModeHandler().compute(loop) == 0.0
            assert loop._state_machine.mode_state == ChargeModeState.ECO_NIGHT_FLOOR_STOP

    def test_discharge_limit_trip_holds_floor_without_cooldown(self):
        loop, _ = _night_loop(battery_w=-17000.0, solar_battery_max_discharge_w=5000.0)
        assert EcoNightModeHandler().compute(loop) == _MIN_CHARGE_W
        assert loop._eco_night_stopped_at is None

    def test_discharge_limit_trip_without_floor_stops_with_cooldown(self):
        loop, _ = _night_loop(min_charge=False, battery_w=-17000.0, solar_battery_max_discharge_w=5000.0)
        assert EcoNightModeHandler().compute(loop) == 0.0
        assert loop._eco_night_stopped_at is not None

    def test_night_rate_used_when_higher_than_floor(self):
        loop, _ = _night_loop(solar_battery_max_ev_charge_power_w=7000.0)
        assert EcoNightModeHandler().compute(loop) == 7000.0

    def test_grid_fallback_never_below_floor(self, monkeypatch):
        import app.control.mode_strategies as ms

        monkeypatch.setattr(ms, "compute_grid_fallback_setpoint", lambda loop, soc: 0.0)  # e.g. < 60 s to window end
        loop, _ = _night_loop(home_soc=16.0, battery_w=0.0)
        assert EcoNightModeHandler().compute(loop) == _MIN_CHARGE_W
        assert loop._state_machine.mode_state == ChargeModeState.ECO_NIGHT_GRID_FALLBACK
        loop, _ = _night_loop(min_charge=False, home_soc=16.0, battery_w=0.0)
        assert EcoNightModeHandler().compute(loop) == 0.0

    def test_floor_stop_unchanged_once_min_soc_reached(self):
        loop, _ = _night_loop(ev_soc=55.0, home_soc=16.0, battery_w=0.0)
        assert EcoNightModeHandler().compute(loop) == 0.0
        assert loop._state_machine.mode_state == ChargeModeState.ECO_NIGHT_FLOOR_STOP
