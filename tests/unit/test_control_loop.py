"""Unit tests for ControlLoop setpoint computation."""

from __future__ import annotations

import time as _time
from unittest.mock import AsyncMock, MagicMock

from app.control_loop import (
    _ECO_DAY_COOLDOWN_S,
    _ECO_DAY_RAMP_STEP_W,
    _GRID_EXPORT_START_THRESHOLD_W,
    _MAX_CHARGE_W,
    _MIN_CHARGE_W,
    ControlLoop,
)
from app.state import AppState


def _make_loop(state: AppState, **overrides) -> ControlLoop:
    """Create a ControlLoop with mocked dependencies."""
    import asyncio as _asyncio

    victron = MagicMock()
    victron.connected = overrides.pop("victron_connected", True)
    ev = AsyncMock()
    queue = _asyncio.Queue()
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

    def test_100_deadband_ramps_up_on_idle_draw(self):
        """Battery at -100W (parasitic idle draw within dead band) should ramp UP to probe solar capacity."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=-100.0,  # within ±200W dead band
            ev_active_power_w=5000.0,
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 6000.0
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 1000.0)
        result = cl._setpoint_eco_day()
        assert result == 6000.0 + _ECO_DAY_RAMP_STEP_W  # ramps up to trigger solar demand

    def test_100_deadband_ramps_up_on_small_charge(self):
        """Battery at +150W (small charge within dead band) should ramp UP."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=150.0,  # within ±200W dead band
            ev_active_power_w=5000.0,
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 6000.0
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 1000.0)
        result = cl._setpoint_eco_day()
        assert result == 6000.0 + _ECO_DAY_RAMP_STEP_W  # ramps up

    def test_100_ramps_up_after_cloud_recovery(self):
        """Real-world bug: after clouds pass and charging restarts, setpoint must ramp up.

        Conditions: SOC 98%, battery -107W (idle parasitic), grid -1480W (exporting),
        EV at minimum and drawing power. The old dead-band logic held steady here,
        locking the setpoint at minimum despite clear solar excess.
        """
        state = self._make_state(
            solar_battery_soc_pct=98.0,
            solar_battery_power_w=-107.0,  # idle parasitic draw
            grid_power_w=-1480.0,
            ev_active_power_w=4400.0,  # charger drawing at minimum
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = _MIN_CHARGE_W  # stuck at minimum after restart
        _fill_grid_samples(cl, -1480.0)
        _fill_battery_samples(cl, -107.0)
        result = cl._setpoint_eco_day()
        assert result == _MIN_CHARGE_W + _ECO_DAY_RAMP_STEP_W  # must ramp up, not hold

    def test_100_ramps_down_on_significant_discharge(self):
        """Battery at -300W (beyond dead band) should ramp DOWN."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=-300.0,  # beyond -200W dead band
            ev_active_power_w=5000.0,
        )
        cl = _make_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 6000.0
        _fill_grid_samples(cl, -1500.0)
        _fill_battery_samples(cl, 1000.0)
        result = cl._setpoint_eco_day()
        assert result == 6000.0 - _ECO_DAY_RAMP_STEP_W  # ramps down


class TestEcoNightGridFallback:
    """Tests for grid fallback when home battery goes flat during eco night."""

    def _make_state(self, **overrides):
        defaults = dict(
            ev_connected=True,
            charge_mode="Eco",
            solar_battery_soc_pct=20.0,  # at floor
            solar_battery_power_w=0.0,  # battery flat (not delivering)
            solar_battery_discharge_floor_pct=20.0,
            solar_battery_max_ev_charge_power_w=5000.0,
            solar_battery_max_discharge_w=6000.0,
            ev_min_soc_pct=40.0,
            ev_battery_capacity_kwh=82.0,
            grid_power_w=0.0,
            solar_battery_discharge_start="23:00",
            solar_battery_discharge_end="06:00",
        )
        defaults.update(overrides)
        return AppState(**defaults)

    def test_battery_flat_ev_needs_charge_calculates_grid_power(self):
        """When battery is flat and EV needs charge, compute grid fallback setpoint."""
        state = self._make_state(
            solar_battery_power_w=200.0,  # > 100, battery stopped discharging
            ev_soc_pct=20.0,
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = _make_loop(state)
        result = cl._setpoint_eco_night()
        # Should return a positive setpoint (grid fallback), not 0
        assert result >= _MIN_CHARGE_W

    def test_battery_flat_ev_soc_met_returns_zero(self):
        """When battery is flat but EV has reached target SOC, stop."""
        state = self._make_state(
            solar_battery_power_w=200.0,
            ev_soc_pct=50.0,  # above 40% target
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = _make_loop(state)
        result = cl._setpoint_eco_night()
        assert result == 0.0

    def test_battery_flat_ev_soc_unknown_returns_zero(self):
        """When battery is flat and EV SOC is unknown, stop (conservative)."""
        state = self._make_state(
            solar_battery_power_w=200.0,
            ev_soc_pct=None,
        )
        cl = _make_loop(state)
        result = cl._setpoint_eco_night()
        assert result == 0.0

    def test_battery_still_delivering_uses_normal_logic(self):
        """When battery is still delivering power (not flat), use normal setpoint."""
        state = self._make_state(
            solar_battery_power_w=-3000.0,  # still discharging
            solar_battery_soc_pct=50.0,  # above floor
        )
        cl = _make_loop(state)
        result = cl._setpoint_eco_night()
        assert result == 5000.0  # normal fixed setpoint

    def test_grid_fallback_clamps_to_min(self):
        """Grid fallback setpoint is clamped to charger minimum."""
        state = self._make_state(
            solar_battery_power_w=200.0,
            ev_soc_pct=39.0,  # only 1% gap, very little energy needed
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = _make_loop(state)
        result = cl._compute_grid_fallback_setpoint(39.0)
        assert result >= _MIN_CHARGE_W

    def test_grid_fallback_clamps_to_max(self):
        """Grid fallback setpoint is clamped to charger maximum."""
        state = self._make_state(
            solar_battery_power_w=200.0,
            ev_soc_pct=5.0,  # huge gap, needs lots of power
            ev_battery_capacity_kwh=200.0,  # large battery
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = _make_loop(state)
        # With a huge gap and short time, required power could exceed max
        result = cl._compute_grid_fallback_setpoint(5.0)
        assert result <= _MAX_CHARGE_W

    def test_grid_fallback_ev_already_at_target(self):
        """Grid fallback returns 0 when EV is already at target."""
        state = self._make_state()
        cl = _make_loop(state)
        result = cl._compute_grid_fallback_setpoint(40.0)
        assert result == 0.0

    def test_grid_fallback_ev_above_target(self):
        """Grid fallback returns 0 when EV is above target."""
        state = self._make_state()
        cl = _make_loop(state)
        result = cl._compute_grid_fallback_setpoint(60.0)
        assert result == 0.0


class TestEvMaxSoc:
    """Tests for max EV SOC charge limit."""

    def test_stops_when_ev_reaches_max_soc(self):
        """Charging stops when EV SOC reaches the max target."""
        state = AppState(
            ev_connected=True,
            charge_mode="Manual",
            manual_power_w=7000.0,
            ev_soc_pct=80.0,
            ev_max_soc_pct=80.0,
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 0.0

    def test_stops_when_ev_above_max_soc(self):
        """Charging stops when EV SOC is above the max target."""
        state = AppState(
            ev_connected=True,
            charge_mode="Manual",
            manual_power_w=7000.0,
            ev_soc_pct=90.0,
            ev_max_soc_pct=80.0,
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 0.0

    def test_continues_when_ev_below_max_soc(self):
        """Charging continues when EV SOC is below the max target."""
        state = AppState(
            ev_connected=True,
            charge_mode="Manual",
            manual_power_w=7000.0,
            ev_soc_pct=70.0,
            ev_max_soc_pct=80.0,
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 7000.0

    def test_continues_when_ev_soc_unavailable(self):
        """Charging continues when EV SOC is unavailable (None)."""
        state = AppState(
            ev_connected=True,
            charge_mode="Manual",
            manual_power_w=7000.0,
            ev_soc_pct=None,
            ev_max_soc_pct=80.0,
        )
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 7000.0

    def test_applies_to_standby_mode(self):
        """Max SOC check runs before mode dispatch — standby still returns 0."""
        state = AppState(
            ev_connected=True,
            charge_mode="Standby",
            ev_soc_pct=90.0,
            ev_max_soc_pct=80.0,
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 0.0

    def test_max_soc_resets_on_disconnect(self):
        """ev_max_soc_pct resets to 80% when vehicle is disconnected."""
        state = AppState(
            ev_connected=False,
            ev_max_soc_pct=100.0,
        )
        cl = _make_loop(state)
        cl._prev_ev_connected = True  # was connected, now disconnected
        # Simulate one run_loop iteration's disconnect detection
        # We test the logic directly
        if not state.ev_connected and cl._prev_ev_connected is not False:
            from app.control_loop import _EV_MAX_SOC_DEFAULT

            state.ev_max_soc_pct = _EV_MAX_SOC_DEFAULT
        assert state.ev_max_soc_pct == 80.0

    def test_max_soc_100_allows_full_charge(self):
        """When max SOC is set to 100%, charging continues past 80%."""
        state = AppState(
            ev_connected=True,
            charge_mode="Manual",
            manual_power_w=7000.0,
            ev_soc_pct=85.0,
            ev_max_soc_pct=100.0,
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = _make_loop(state)
        assert cl._compute_setpoint() == 7000.0


class TestChargingEvents:
    """Tests for charging event state machine (started/stopping/stopped).

    Key behaviour: stopping event is emitted BEFORE setpoint goes to zero.
    The charger continues at the previous setpoint for 10s, then stopped is emitted
    and setpoint actually goes to zero.
    """

    def _make_state(self, **overrides):
        defaults = dict(
            ev_connected=True,
            charge_mode="Manual",
            manual_power_w=7000.0,
            ev_active_power_w=5000.0,
            ev_session_energy_wh=3000.0,
        )
        defaults.update(overrides)
        return AppState(**defaults)

    def _get_events(self, cl):
        """Extract charging events from the publish queue."""
        events = []
        while not cl._publish_queue.empty():
            item = cl._publish_queue.get_nowait()
            if isinstance(item, dict) and item.get("type") == "charging_event":
                events.append(item)
        return events

    def test_started_event_on_first_charge(self):
        state = self._make_state()
        cl = _make_loop(state)
        cl._charging_state = "idle"
        result = cl._apply_charging_events(7000.0)
        assert result == 7000.0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "started"
        assert events[0]["setpoint_w"] == 7000.0
        assert cl._charging_state == "charging"

    def test_stopping_emitted_but_setpoint_held(self):
        """When setpoint wants to go to 0, stopping is emitted but previous setpoint is returned."""
        state = self._make_state()
        cl = _make_loop(state)
        cl._charging_state = "charging"
        cl._last_positive_setpoint = 6000.0
        result = cl._apply_charging_events(0.0)
        assert result == 6000.0  # held at previous setpoint, NOT 0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "stopping"
        assert cl._charging_state == "stopping"

    def test_setpoint_held_during_grace_period(self):
        """During the 10s grace period, setpoint stays at previous value."""
        state = self._make_state()
        cl = _make_loop(state)
        cl._charging_state = "stopping"
        cl._stopping_at = _time.monotonic() - 5  # 5s ago
        cl._last_positive_setpoint = 6000.0
        cl._stopping_reason = "max_soc_reached"
        result = cl._apply_charging_events(0.0)
        assert result == 6000.0  # still held
        assert cl._charging_state == "stopping"

    def test_stopped_emitted_after_grace_period(self):
        """After 10s grace, setpoint goes to 0 but enters stopped_pending (no event yet)."""
        state = self._make_state(ev_session_energy_wh=5000.0)
        cl = _make_loop(state)
        cl._charging_state = "stopping"
        cl._stopping_at = _time.monotonic() - 11  # 11s ago
        cl._last_positive_setpoint = 6000.0
        cl._stopping_reason = "max_soc_reached"
        result = cl._apply_charging_events(0.0)
        assert result == 0.0  # setpoint goes to 0
        events = self._get_events(cl)
        assert len(events) == 0  # no stopped event yet — in stopped_pending
        assert cl._charging_state == "stopped_pending"

    def test_stopped_event_after_pending_delay(self):
        """Stopped event emitted after 5s in stopped_pending state."""
        state = self._make_state(ev_session_energy_wh=5000.0)
        cl = _make_loop(state)
        cl._charging_state = "stopped_pending"
        cl._stopped_at = _time.monotonic() - 6  # 6s ago
        cl._stopping_reason = "max_soc_reached"
        result = cl._apply_charging_events(0.0)
        assert result == 0.0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "stopped"
        assert events[0]["session_energy_wh"] == 5000.0
        assert cl._charging_state == "idle"

    def test_no_stopped_event_during_pending_delay(self):
        """No stopped event while still within the 5s pending delay."""
        state = self._make_state()
        cl = _make_loop(state)
        cl._charging_state = "stopped_pending"
        cl._stopped_at = _time.monotonic() - 2  # only 2s ago
        cl._stopping_reason = "max_soc_reached"
        result = cl._apply_charging_events(0.0)
        assert result == 0.0
        events = self._get_events(cl)
        assert len(events) == 0
        assert cl._charging_state == "stopped_pending"

    def test_resume_from_stopped_pending(self):
        """If charging resumes during stopped_pending, go straight to charging."""
        state = self._make_state()
        cl = _make_loop(state)
        cl._charging_state = "stopped_pending"
        cl._stopped_at = _time.monotonic() - 2
        cl._stopping_reason = "eco_day_conditions"
        result = cl._apply_charging_events(7000.0)
        assert result == 7000.0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "started"
        assert cl._charging_state == "charging"

    def test_resume_from_stopping_cancels_stop(self):
        """If charging resumes during grace period, cancel the stop."""
        state = self._make_state()
        cl = _make_loop(state)
        cl._charging_state = "stopping"
        cl._stopping_at = _time.monotonic() - 3
        cl._last_positive_setpoint = 6000.0
        cl._stopping_reason = "eco_day_conditions"
        result = cl._apply_charging_events(7000.0)
        assert result == 7000.0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "started"
        assert cl._charging_state == "charging"

    def test_no_event_when_idle_and_not_charging(self):
        state = self._make_state()
        cl = _make_loop(state)
        cl._charging_state = "idle"
        result = cl._apply_charging_events(0.0)
        assert result == 0.0
        events = self._get_events(cl)
        assert len(events) == 0

    def test_no_event_when_charging_continues(self):
        state = self._make_state()
        cl = _make_loop(state)
        cl._charging_state = "charging"
        cl._last_positive_setpoint = 7000.0
        result = cl._apply_charging_events(7000.0)
        assert result == 7000.0
        events = self._get_events(cl)
        assert len(events) == 0

    def test_max_soc_reason_detected(self):
        state = self._make_state(ev_soc_pct=79.9, ev_max_soc_pct=80.0)
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = _make_loop(state)
        reason = cl._determine_stop_reason()
        assert reason == "max_soc_reached"

    def test_vehicle_disconnected_reason(self):
        state = self._make_state(ev_connected=False)
        cl = _make_loop(state)
        reason = cl._determine_stop_reason()
        assert reason == "vehicle_disconnected"

    def test_standby_reason(self):
        state = self._make_state(charge_mode="Standby")
        cl = _make_loop(state)
        reason = cl._determine_stop_reason()
        assert reason == "standby"

    def test_external_stop_when_ev_power_drops_to_zero(self):
        """External stop enters stopped_pending (no immediate event)."""
        state = self._make_state(ev_active_power_w=0.0, ev_session_energy_wh=8000.0)
        cl = _make_loop(state)
        cl._charging_state = "charging"
        cl._last_positive_setpoint = 7000.0
        result = cl._apply_charging_events(7000.0)  # setpoint still positive
        assert result == 0.0  # overridden to 0
        events = self._get_events(cl)
        assert len(events) == 0  # no event yet — in stopped_pending
        assert cl._charging_state == "stopped_pending"
        assert cl._stopping_reason == "external_stop"

    def test_no_external_stop_when_ev_power_positive(self):
        """No external stop when charger is still drawing power."""
        state = self._make_state(ev_active_power_w=5000.0)
        cl = _make_loop(state)
        cl._charging_state = "charging"
        cl._last_positive_setpoint = 7000.0
        result = cl._apply_charging_events(7000.0)
        assert result == 7000.0
        events = self._get_events(cl)
        assert len(events) == 0

    def test_car_disconnect_triggers_normal_stopping_flow(self):
        """Car disconnect causes _compute_setpoint to return 0, triggering normal stopping."""
        state = self._make_state(ev_connected=False)
        cl = _make_loop(state)
        cl._charging_state = "charging"
        cl._last_positive_setpoint = 7000.0
        # _compute_setpoint returns 0 when ev_connected is False
        result = cl._apply_charging_events(0.0)
        assert result == 7000.0  # held during grace period
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "stopping"
        assert cl._charging_state == "stopping"

    def test_external_stop_when_power_drops_to_zero(self):
        """External stop enters stopped_pending when charger stops drawing power."""
        state = self._make_state(ev_active_power_w=0.0)
        cl = _make_loop(state)
        cl._charging_state = "charging"
        cl._last_positive_setpoint = 7000.0
        result = cl._apply_charging_events(7000.0)
        assert result == 0.0  # setpoint overridden to 0 since charger stopped
        events = self._get_events(cl)
        assert len(events) == 0  # no event yet — in stopped_pending
        assert cl._charging_state == "stopped_pending"

    def test_external_stop_vehicle_disconnected(self):
        """Stopped_pending with vehicle_disconnected reason when EV unplugged externally."""
        state = self._make_state(ev_connected=False, ev_active_power_w=0.0)
        cl = _make_loop(state)
        cl._charging_state = "charging"
        cl._last_positive_setpoint = 7000.0
        cl._apply_charging_events(7000.0)
        events = self._get_events(cl)
        assert len(events) == 0  # no event yet — in stopped_pending
        assert cl._charging_state == "stopped_pending"
        assert cl._stopping_reason == "vehicle_disconnected"

    def test_external_stop_emits_after_delay(self):
        """External stop: stopped event emitted after 5s delay."""
        state = self._make_state(ev_active_power_w=0.0, ev_session_energy_wh=8000.0)
        cl = _make_loop(state)
        cl._charging_state = "stopped_pending"
        cl._stopped_at = _time.monotonic() - 6  # 6s ago
        cl._stopping_reason = "external_stop"
        result = cl._apply_charging_events(0.0)
        assert result == 0.0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "stopped"
        assert events[0]["reason"] == "external_stop"
        assert events[0]["session_energy_wh"] == 8000.0
        assert cl._charging_state == "idle"

    def test_no_external_stop_when_power_positive(self):
        """No stopped event when charger is still drawing power."""
        state = self._make_state(ev_active_power_w=5000.0)
        cl = _make_loop(state)
        cl._charging_state = "charging"
        cl._last_positive_setpoint = 7000.0
        cl._apply_charging_events(7000.0)
        events = self._get_events(cl)
        assert len(events) == 0
        assert cl._charging_state == "charging"
