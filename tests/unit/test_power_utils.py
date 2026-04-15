"""Direct unit tests for power utility helpers."""

from __future__ import annotations

import time as _time

from app.control.constants import _MAX_CHARGE_W, _MIN_CHARGE_W
from app.control.power_utils import (
    clamp,
    compute_grid_fallback_setpoint,
    get_ev_soc,
    limit_battery_discharge,
    mean_battery_power,
    mean_grid_power,
    prune_samples,
    record_samples,
)
from app.state import AppState
from tests.unit.helpers import make_ns_loop


class TestClamp:
    def test_clamps_to_bounds(self):
        assert clamp(1000.0, _MIN_CHARGE_W, _MAX_CHARGE_W) == _MIN_CHARGE_W
        assert clamp(50000.0, _MIN_CHARGE_W, _MAX_CHARGE_W) == _MAX_CHARGE_W


class TestGetEvSoc:
    def test_returns_fresh_value(self):
        state = AppState(ev_soc_pct=42.0)
        state.ev_soc_pct_updated_at = _time.monotonic()
        loop = make_ns_loop(state)

        assert get_ev_soc(loop) == 42.0

    def test_returns_none_when_stale(self):
        state = AppState(ev_soc_pct=42.0)
        state.ev_soc_pct_updated_at = _time.monotonic() - 10_000.0
        loop = make_ns_loop(state)

        assert get_ev_soc(loop) is None


class TestRollingSamples:
    def test_record_and_mean_samples(self):
        state = AppState(grid_power_w=-1200.0, solar_battery_power_w=-800.0)
        loop = make_ns_loop(state)

        record_samples(loop)

        assert mean_grid_power(loop) == -1200.0
        assert mean_battery_power(loop) == -800.0

    def test_prune_samples_drops_old_values(self):
        state = AppState(eco_mean_window_minutes=1)
        now = _time.monotonic()
        loop = make_ns_loop(
            state,
            _grid_power_samples=[(now - 120.0, -1000.0), (now - 5.0, -500.0)],
            _battery_power_samples=[(now - 120.0, -900.0), (now - 5.0, -400.0)],
        )

        prune_samples(loop)

        assert loop._grid_power_samples == [(loop._grid_power_samples[0][0], -500.0)]
        assert loop._battery_power_samples == [(loop._battery_power_samples[0][0], -400.0)]


class TestGridFallback:
    def test_invalid_end_time_returns_min_charge(self):
        state = AppState(
            ev_min_soc_pct=40.0,
            ev_battery_capacity_kwh=82.0,
            solar_battery_discharge_end="bad",
        )
        loop = make_ns_loop(state)

        assert compute_grid_fallback_setpoint(loop, 20.0) == _MIN_CHARGE_W

    def test_large_gap_clamps_to_max(self):
        state = AppState(
            ev_min_soc_pct=90.0,
            ev_battery_capacity_kwh=500.0,
            solar_battery_discharge_end="00:01",
        )
        loop = make_ns_loop(state)

        result = compute_grid_fallback_setpoint(loop, 5.0)

        assert result <= _MAX_CHARGE_W


class TestBatteryDischargeLimit:
    def test_reduces_setpoint_when_discharge_limit_exceeded(self):
        state = AppState(solar_battery_power_w=-7000.0)
        loop = make_ns_loop(state)

        assert limit_battery_discharge(loop, 7000.0, 6000.0) == 6000.0

    def test_returns_zero_when_reduction_falls_below_minimum(self):
        state = AppState(solar_battery_power_w=-9000.0)
        loop = make_ns_loop(state)

        assert limit_battery_discharge(loop, 4000.0, 6000.0) == 0.0
