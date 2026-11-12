"""Eco-day anti-windup: the ramp must not run away from what the limiter / the car actually allow."""

from __future__ import annotations

import pytest

from app.control import breaker
from app.control.breaker import limit_phase_current, record_phase_samples
from app.control.constants import _ECO_DAY_RAMP_STEP_W, _MAX_CHARGE_W, _MIN_CHARGE_W
from app.control.mode_strategies import clamp_eco_day_to_applied, setpoint_eco_day
from app.log_throttle import LogThrottle
from app.state import ChargeModeState
from tests.unit.breaker_helpers import P_LIM, make_state, set_inputs
from tests.unit.helpers import fill_battery_samples, fill_grid_samples, make_control_loop

CYCLE_S = 10.0


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch):
    monkeypatch.setattr(breaker, "_throttle", LogThrottle(breaker.logger))


def _eco_loop(**overrides):
    state = make_state(
        ev_connected=True,
        charge_mode="Eco",
        solar_battery_soc_pct=100.0,
        solar_battery_power_w=2000.0,
        grid_power_w=-1500.0,
        ev_active_power_w=4400.0,
        eco_day_min_solar_battery_soc_pct=90.0,
        solar_battery_discharge_start="00:00",
        solar_battery_discharge_end="00:01",
        **overrides,
    )
    cl = make_control_loop(state)
    cl._eco_charging = True
    cl._eco_day_setpoint_w = _MIN_CHARGE_W
    fill_grid_samples(cl, -1500.0)
    fill_battery_samples(cl, 2000.0)
    return cl, state


class TestClampToApplied:
    def test_clamps_only_while_ramping(self):
        cl, _ = _eco_loop()
        cl._eco_day_setpoint_w = 9000.0
        cl._charge_mode_state = ChargeModeState.ECO_DAY_MINIMUM
        clamp_eco_day_to_applied(cl, 9000.0, 5000.0)
        assert cl._eco_day_setpoint_w == 9000.0
        cl._charge_mode_state = ChargeModeState.ECO_DAY_RAMPING
        clamp_eco_day_to_applied(cl, 9000.0, 5000.0)
        assert cl._eco_day_setpoint_w == 5000.0

    def test_noop_when_not_reduced(self):
        cl, _ = _eco_loop()
        cl._charge_mode_state = ChargeModeState.ECO_DAY_RAMPING
        cl._eco_day_setpoint_w = 9000.0
        clamp_eco_day_to_applied(cl, 9000.0, 9000.0)
        assert cl._eco_day_setpoint_w == 9000.0

    def test_minimum_is_preserved_and_stop_clamps_to_minimum(self):
        cl, _ = _eco_loop()
        cl._charge_mode_state = ChargeModeState.ECO_DAY_RAMPING
        cl._eco_day_setpoint_w = 9000.0
        clamp_eco_day_to_applied(cl, 9000.0, 0.0)
        assert cl._eco_day_setpoint_w == _MIN_CHARGE_W


class TestEcoDayAntiWindup:
    def test_ramp_unchanged_when_raw_ev_currents_unknown(self):
        cl, _ = _eco_loop()
        cl._eco_day_setpoint_w = 5000.0
        assert setpoint_eco_day(cl) == 5000.0 + _ECO_DAY_RAMP_STEP_W

    def test_ramp_reaches_max_when_plant_follows(self):
        """Three-phase car delivering setpoint/3 on every phase: no artificial limit."""
        cl, state = _eco_loop()
        requested = _MIN_CHARGE_W
        for _ in range(150):
            per_phase = requested / 3.0
            set_inputs(state, (500.0, 500.0, 500.0), ev_w=(per_phase,) * 3)
            requested = setpoint_eco_day(cl)
        assert requested == _MAX_CHARGE_W

    def test_ev_not_following_does_not_wind_up_to_max(self):
        """Car limited to 3 kW on its phase: setpoint stays within one step of 3 x measured."""
        cl, state = _eco_loop()
        requested = _MIN_CHARGE_W
        for _ in range(150):
            p_ev = min(requested / 3.0, 3000.0)
            set_inputs(state, (500.0, -500.0, -500.0), ev_w=(p_ev, 0.0, 0.0))
            requested = setpoint_eco_day(cl)
            assert requested <= max(_MIN_CHARGE_W, 3 * 3000.0 + _ECO_DAY_RAMP_STEP_W)
        assert requested < 10000.0

    def test_minimum_preserved_when_ev_barely_draws(self):
        cl, state = _eco_loop()
        set_inputs(state, (0.0, 0.0, 0.0), ev_w=(300.0, 0.0, 0.0))
        cl._eco_day_setpoint_w = _MIN_CHARGE_W
        assert setpoint_eco_day(cl) == _MIN_CHARGE_W

    def test_cap_active_setpoint_does_not_climb(self):
        """Full loop of ramp -> limiter -> clamp with a single-phase car and a loaded L1."""
        cl, state = _eco_loop()
        base = 3000.0
        applied = _MIN_CHARGE_W
        requested_hist = []
        t = 0.0
        for _ in range(80):
            p_ev = min(applied / 3.0, 7360.0)
            set_inputs(state, (base + p_ev, -1000.0, -1000.0), ev_w=(p_ev, 0.0, 0.0))
            requested = setpoint_eco_day(cl)
            record_phase_samples(cl, now=t)
            applied = limit_phase_current(cl, requested, now=t)
            clamp_eco_day_to_applied(cl, requested, applied)
            requested_hist.append(requested)
            t += CYCLE_S
        cap = 3 * (P_LIM - base)
        # Without anti-windup the ramp would be at 22 kW by now
        assert max(requested_hist[-30:]) <= cap + _ECO_DAY_RAMP_STEP_W + 1.0
        assert cl._eco_day_setpoint_w <= cap + 1.0
        assert applied == pytest.approx(cap, abs=_ECO_DAY_RAMP_STEP_W + 1.0)
