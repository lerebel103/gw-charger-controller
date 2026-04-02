"""Unit tests for ControlLoop setpoint computation."""

from __future__ import annotations

import time as _time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.control_loop import (
    ControlLoop,
    _ECO_DAY_COOLDOWN_S,
    _ECO_DAY_RAMP_STEP_W,
    _GRID_EXPORT_START_THRESHOLD_W,
    _MAX_CHARGE_W,
    _MIN_CHARGE_W,
)
from app.state import AppState


def _make_loop(state: AppState, **overrides) -> ControlLoop:
    """Create a ControlLoop with mocked dependencies."""
    victron = MagicMock()
    victron.connected = overrides.pop("victron_connected", True)
    ev = AsyncMock()
    queue = AsyncMock()
    return ControlLoop(state, victron, ev, queue)


def _fill_grid_samples(cl: ControlLoop, value: float, count: int = 60):
    """Fill the grid power rolling buffer with a constant value."""
    now = _time.monotonic()
    cl._grid_power_samples = [(now - i, value) for i in range(count)]


def _fill_battery_samples(cl: ControlLoop, value: float, count: int = 60):
    """Fill the battery power rolling buffer with a constant value."""
    now = _time.monotonic()
    cl._battery_power_samples = [(now - i, value) for i in range(count)]


# ---------------------------------------------------------------------------
# _compute_setpoint dispatch
# ---------------------------------------------------------------------------

class TestComputeSetpoint:
    def test_no_vehicle_returns_zero(self):
        state = AppState(ev_connected=False, charge_mode="Eco")
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 0.0

    def test_manual_mode(self):
        state = AppState(ev_connected=True, charge_mode="Manual", manual_power_w=7000.0)
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 7000.0

    def test_manual_mode_clamps_low(self):
        state = AppState(ev_connected=True, charge_mode="Manual", manual_power_w=1000.0)
        cl = _make_loop(state)
        assert cl._compute_setpoint() == _MIN_CHARGE_W

    def test_manual_mode_clamps_high(self):
        state = AppState(ev_connected=True, charge_mode="Manual", manual_power_w=50000.0)
        cl = _make_loop(state)
        assert cl._compute_setpoint() == _MAX_CHARGE_W

    def test_standby_returns_zero(self):
        state = AppState(ev_connected=True, charge_mode="Standby")
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 0.0

    def test_eco_victron_down_returns_zero(self):
        state = AppState(ev_connected=True, charge_mode="Eco")
        cl = _make_loop(state, victron_connected=False)
        assert cl._compute_setpoint() == 0.0


# ---------------------------------------------------------------------------
# _setpoint_eco_day
# ---------------------------------------------------------------------------

class TestSetpointEcoDay:
    """Tests for eco day logic (outside discharge window)."""

    def _make_eco_day_state(self, **overrides):
        defaults = dict(
            ev_connected=True,
            charge_mode="Eco",
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=2000.0,
            grid_power_w=-1500.0,
            ev_active_power_w=4400.0,
            eco_day_min_battery_soc_pct=90.0,
            solar_battery_day_power_limit_w=-1500.0,
            solar_battery_discharge_start="23:00",
            solar_battery_discharge_end="06:00",
        )
        defaults.update(overrides)
        return AppState(**defaults)

    # --- SOC gate ---

    def test_soc_below_threshold_returns_zero(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=80.0)
        cl = _make_loop(state)
        assert cl._setpoint_eco_day() == 0.0

    def test_soc_at_threshold_passes_gate(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=90.0)
        cl = _make_loop(state)
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 2000.0)
        # Should not return 0 from SOC gate
        result = cl._setpoint_eco_day()
        assert result > 0

    # --- Cooldown ---

    def test_cooldown_prevents_restart(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=100.0)
        cl = _make_loop(state)
        cl._eco_charging = False
        cl._eco_day_stopped_at = _time.monotonic()  # just stopped
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 2000.0)
        assert cl._setpoint_eco_day() == 0.0

    def test_cooldown_expired_allows_restart(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=100.0)
        cl = _make_loop(state)
        cl._eco_charging = False
        cl._eco_day_stopped_at = _time.monotonic() - _ECO_DAY_COOLDOWN_S - 1
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 2000.0)
        result = cl._setpoint_eco_day()
        assert result > 0
        assert cl._eco_charging is True

    # --- Mean grid start ---

    def test_no_start_when_grid_above_threshold(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=100.0)
        cl = _make_loop(state)
        _fill_grid_samples(cl, -500.0)  # not enough export
        _fill_battery_samples(cl, 2000.0)
        assert cl._setpoint_eco_day() == 0.0

    def test_starts_when_grid_at_threshold(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=100.0)
        cl = _make_loop(state)
        _fill_grid_samples(cl, _GRID_EXPORT_START_THRESHOLD_W)
        _fill_battery_samples(cl, 2000.0)
        result = cl._setpoint_eco_day()
        assert result > 0
        assert cl._eco_charging is True

    # --- Mean battery stop ---

    def test_stops_on_sustained_battery_discharge(self):
        state = self._make_eco_day_state(
            solar_battery_soc_pct=100.0,
            solar_battery_day_power_limit_w=-1500.0,
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, -2000.0)  # below -1500 limit
        result = cl._setpoint_eco_day()
        assert result == 0.0
        assert cl._eco_charging is False
        assert cl._eco_day_stopped_at is not None

    def test_continues_when_battery_above_limit(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=100.0)
        cl = _make_loop(state)
        cl._eco_charging = True
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, -500.0)  # above -1500 limit
        result = cl._setpoint_eco_day()
        assert result > 0

    # --- 90-99% SOC: minimum lock with safeguards ---

    def test_90_99_returns_minimum_when_charging_started(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=95.0)
        cl = _make_loop(state)
        cl._eco_charging = True
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 2000.0)  # battery healthy
        result = cl._setpoint_eco_day()
        assert result == _MIN_CHARGE_W

    def test_90_99_respects_mean_battery_stop(self):
        state = self._make_eco_day_state(
            solar_battery_soc_pct=95.0,
            solar_battery_day_power_limit_w=-1500.0,
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, -2000.0)  # sustained discharge
        result = cl._setpoint_eco_day()
        assert result == 0.0
        assert cl._eco_charging is False

    def test_90_99_respects_cooldown(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=95.0)
        cl = _make_loop(state)
        cl._eco_charging = False
        cl._eco_day_stopped_at = _time.monotonic()
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 2000.0)
        assert cl._setpoint_eco_day() == 0.0

    def test_90_99_respects_grid_start_threshold(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=95.0)
        cl = _make_loop(state)
        _fill_grid_samples(cl, -500.0)  # not enough export
        _fill_battery_samples(cl, 2000.0)
        assert cl._setpoint_eco_day() == 0.0

    # --- 100% SOC: ramp ---

    def test_100_ramps_up(self):
        state = self._make_eco_day_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=2000.0,
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 5000.0
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 2000.0)
        result = cl._setpoint_eco_day()
        assert result == 5000.0 + _ECO_DAY_RAMP_STEP_W

    def test_100_ramps_down_on_discharge(self):
        state = self._make_eco_day_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=-500.0,
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 6000.0
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 500.0)  # mean still positive
        result = cl._setpoint_eco_day()
        assert result == 6000.0 - _ECO_DAY_RAMP_STEP_W

    def test_100_ramp_clamps_to_min(self):
        state = self._make_eco_day_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=-500.0,
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = _MIN_CHARGE_W
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 500.0)
        result = cl._setpoint_eco_day()
        assert result == _MIN_CHARGE_W  # clamped, not below

    def test_100_ramp_clamps_to_max(self):
        state = self._make_eco_day_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=5000.0,
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = _MAX_CHARGE_W
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 5000.0)
        result = cl._setpoint_eco_day()
        assert result == _MAX_CHARGE_W  # clamped, not above


# ---------------------------------------------------------------------------
# Scenario tests based on real system parameters:
#   Max solar: 8200 W
#   House load: ~2000 W
#   Grid export cap: ~1500 W
#   Solar battery charge max: 4500 W
#   Solar battery max discharge: 6000 W
#   EV charger min: 4400 W, max: 22000 W
#   eco_day_min_battery_soc_pct: 90%
#   solar_battery_day_power_limit_w: -1500 W
#   solar_battery_discharge_floor_pct: 20%
#   ev_min_soc_pct: 40%
# ---------------------------------------------------------------------------


class TestManualModeScenarios:
    """Manual mode: fixed power regardless of solar/battery state."""

    def _make_state(self, **overrides):
        defaults = dict(
            ev_connected=True,
            charge_mode="Manual",
            manual_power_w=7000.0,
            solar_battery_soc_pct=50.0,
            solar_battery_power_w=-2000.0,  # battery discharging
            grid_power_w=3000.0,  # importing from grid
        )
        defaults.update(overrides)
        return AppState(**defaults)

    def test_charges_at_configured_power(self):
        state = self._make_state(manual_power_w=7000.0)
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 7000.0

    def test_charges_regardless_of_battery_discharge(self):
        state = self._make_state(solar_battery_power_w=-5000.0)
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 7000.0

    def test_charges_regardless_of_grid_import(self):
        state = self._make_state(grid_power_w=5000.0)
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 7000.0

    def test_charges_regardless_of_victron_down(self):
        state = self._make_state()
        cl = _make_loop(state, victron_connected=False)
        assert cl._compute_setpoint() == 7000.0

    def test_no_charge_when_ev_disconnected(self):
        state = self._make_state(ev_connected=False)
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 0.0


class TestStandbyModeScenarios:
    """Standby mode: always zero regardless of conditions."""

    def _make_state(self, **overrides):
        defaults = dict(
            ev_connected=True,
            charge_mode="Standby",
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=0.0,
            grid_power_w=-1500.0,
        )
        defaults.update(overrides)
        return AppState(**defaults)

    def test_returns_zero(self):
        state = self._make_state()
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 0.0

    def test_returns_zero_even_with_excess_solar(self):
        state = self._make_state(grid_power_w=-5000.0, solar_battery_soc_pct=100.0)
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 0.0

    def test_returns_zero_when_ev_disconnected(self):
        state = self._make_state(ev_connected=False)
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 0.0


class TestEcoNightScenarios:
    """Eco night: inside discharge window (23:00-06:00)."""

    def _make_state(self, **overrides):
        defaults = dict(
            ev_connected=True,
            charge_mode="Eco",
            solar_battery_soc_pct=80.0,
            solar_battery_power_w=-3000.0,
            solar_battery_discharge_floor_pct=20.0,
            solar_battery_max_ev_charge_power_w=5000.0,
            solar_battery_max_discharge_w=6000.0,
            ev_min_soc_pct=40.0,
            grid_power_w=0.0,
        )
        defaults.update(overrides)
        return AppState(**defaults)

    def test_charges_at_configured_power(self):
        state = self._make_state()
        cl = _make_loop(state)
        assert cl._setpoint_eco_night() == 5000.0

    def test_stops_at_floor_when_ev_soc_reached(self):
        state = self._make_state(solar_battery_soc_pct=20.0)
        cl = _make_loop(state)
        # ev_soc_pct is None (unknown) — conservative stop
        assert cl._setpoint_eco_night() == 0.0

    def test_continues_at_floor_when_ev_needs_charge(self):
        state = self._make_state(solar_battery_soc_pct=20.0, ev_soc_pct=30.0)
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = _make_loop(state)
        assert cl._setpoint_eco_night() == 5000.0

    def test_stops_at_floor_when_ev_soc_met(self):
        state = self._make_state(solar_battery_soc_pct=20.0, ev_soc_pct=50.0)
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = _make_loop(state)
        assert cl._setpoint_eco_night() == 0.0

    def test_reduces_setpoint_on_excessive_discharge(self):
        state = self._make_state(
            solar_battery_power_w=-8000.0,  # discharging 8kW
            solar_battery_max_discharge_w=6000.0,  # limit 6kW
        )
        cl = _make_loop(state)
        # overshoot = 8000 - 6000 = 2000, setpoint = 5000 - 2000 = 3000 < 4400 min
        assert cl._setpoint_eco_night() == 0.0

    def test_no_reduction_when_within_discharge_limit(self):
        state = self._make_state(
            solar_battery_power_w=-4000.0,
            solar_battery_max_discharge_w=6000.0,
        )
        cl = _make_loop(state)
        assert cl._setpoint_eco_night() == 5000.0


class TestEcoDayRealWorldScenarios:
    """Eco day scenarios with real system parameters.

    System: 8.2kW solar, 1500W export cap, 2kW house load, 4.5kW battery charge.
    """

    def _make_state(self, **overrides):
        defaults = dict(
            ev_connected=True,
            charge_mode="Eco",
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=2000.0,
            grid_power_w=-1500.0,
            ev_active_power_w=4400.0,
            eco_day_min_battery_soc_pct=90.0,
            solar_battery_day_power_limit_w=-1500.0,
            solar_battery_discharge_start="23:00",
            solar_battery_discharge_end="06:00",
        )
        defaults.update(overrides)
        return AppState(**defaults)

    def test_sunny_day_battery_full_ramp_up(self):
        """Battery 100%, exporting 1500W, battery charging 2000W — ramp up."""
        state = self._make_state()
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 5000.0
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 2000.0)
        result = cl._setpoint_eco_day()
        assert result == 5000.0 + _ECO_DAY_RAMP_STEP_W

    def test_cloud_passes_battery_discharges_briefly(self):
        """Battery 100%, instantaneous discharge but mean still healthy — ramp down one step."""
        state = self._make_state(solar_battery_power_w=-500.0)
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 6000.0
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 1000.0)  # mean still positive
        result = cl._setpoint_eco_day()
        assert result == 6000.0 - _ECO_DAY_RAMP_STEP_W

    def test_sustained_cloud_mean_battery_drops(self):
        """Battery 100%, sustained discharge over 5 min — stops with cooldown."""
        state = self._make_state(solar_battery_power_w=-2000.0)
        cl = _make_loop(state)
        cl._eco_charging = True
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, -2000.0)  # mean below -1500 limit
        result = cl._setpoint_eco_day()
        assert result == 0.0
        assert cl._eco_charging is False

    def test_battery_95_pct_returns_minimum(self):
        """Battery 95%, all safeguards pass — returns minimum, not ramp."""
        state = self._make_state(solar_battery_soc_pct=95.0)
        cl = _make_loop(state)
        cl._eco_charging = True
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 2000.0)
        result = cl._setpoint_eco_day()
        assert result == _MIN_CHARGE_W

    def test_battery_95_pct_stops_on_sustained_discharge(self):
        """Battery 95%, sustained discharge — stops even though SOC > 90%."""
        state = self._make_state(
            solar_battery_soc_pct=95.0,
            solar_battery_power_w=-2000.0,
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, -2000.0)
        result = cl._setpoint_eco_day()
        assert result == 0.0

    def test_battery_80_pct_no_charging(self):
        """Battery 80% (below 90% threshold) — no EV charging."""
        state = self._make_state(solar_battery_soc_pct=80.0)
        cl = _make_loop(state)
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 4000.0)
        result = cl._setpoint_eco_day()
        assert result == 0.0

    def test_evening_solar_drops_stops_charging(self):
        """Solar production drops, grid starts importing — doesn't start."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            grid_power_w=500.0,  # importing
        )
        cl = _make_loop(state)
        _fill_grid_samples(cl, 500.0)  # mean is importing
        _fill_battery_samples(cl, 500.0)
        result = cl._setpoint_eco_day()
        assert result == 0.0

    def test_cooldown_after_stop_prevents_immediate_restart(self):
        """After stopping, 5-min cooldown prevents restart even with good conditions."""
        state = self._make_state(solar_battery_soc_pct=100.0)
        cl = _make_loop(state)
        cl._eco_charging = False
        cl._eco_day_stopped_at = _time.monotonic() - 60  # stopped 1 min ago
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 3000.0)
        result = cl._setpoint_eco_day()
        assert result == 0.0  # still in cooldown


    def test_100_no_ramp_when_ev_not_drawing_power(self):
        """When ev_active_power_w is 0 (charger starting up), hold at minimum, don't ramp."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=3000.0,
            ev_active_power_w=0.0,  # charger hasn't started drawing yet
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 6000.0  # was ramped up previously
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 3000.0)
        result = cl._setpoint_eco_day()
        assert result == _MIN_CHARGE_W  # reset to minimum, not ramped further
        assert cl._eco_day_setpoint_w == _MIN_CHARGE_W

    def test_100_no_ramp_when_ev_power_none(self):
        """When ev_active_power_w is None (no reading yet), hold at minimum."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=3000.0,
            ev_active_power_w=None,
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 5000.0
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 3000.0)
        result = cl._setpoint_eco_day()
        assert result == _MIN_CHARGE_W

    def test_100_ramps_when_ev_drawing_power(self):
        """When ev_active_power_w > 0 (charger active), ramp proceeds normally."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=2000.0,
            ev_active_power_w=4400.0,  # charger is drawing power
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 5000.0
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 2000.0)
        result = cl._setpoint_eco_day()
        assert result == 5000.0 + _ECO_DAY_RAMP_STEP_W  # ramped up
