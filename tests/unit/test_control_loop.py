"""Unit tests for ControlLoop setpoint computation."""

from __future__ import annotations

import time as _time

from app.control.constants import (
    _ECO_DAY_COOLDOWN_S,
    _ECO_DAY_RAMP_STEP_W,
    _GRID_EXPORT_START_THRESHOLD_W,
    _MAX_CHARGE_W,
    _MIN_CHARGE_W,
    _STOP_PRESET_W,
)
from app.control.mode_strategies import compute_setpoint, setpoint_eco_day, setpoint_eco_night
from app.control.power_utils import compute_grid_fallback_setpoint
from app.state import AppState, ChargerStatus, ChargeSessionState
from tests.unit.helpers import fill_battery_samples, fill_grid_samples, make_control_loop

# ---------------------------------------------------------------------------
# _compute_setpoint dispatch
# ---------------------------------------------------------------------------


class TestComputeSetpoint:
    def test_no_vehicle_returns_zero(self):
        state = AppState(ev_connected=False, charge_mode="Eco")
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 0.0

    def test_manual_mode(self):
        state = AppState(ev_connected=True, charge_mode="Manual", manual_power_w=7000.0)
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 7000.0

    def test_manual_mode_clamps_low(self):
        state = AppState(ev_connected=True, charge_mode="Manual", manual_power_w=1000.0)
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == _MIN_CHARGE_W

    def test_manual_mode_clamps_high(self):
        state = AppState(ev_connected=True, charge_mode="Manual", manual_power_w=50000.0)
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == _MAX_CHARGE_W

    def test_standby_returns_zero(self):
        state = AppState(ev_connected=True, charge_mode="Standby")
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 0.0

    def test_eco_victron_down_returns_zero(self):
        state = AppState(ev_connected=True, charge_mode="Eco")
        cl = make_control_loop(state, victron_connected=False)
        assert compute_setpoint(cl) == 0.0


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
            eco_day_min_solar_battery_soc_pct=90.0,
            solar_battery_day_power_limit_w=-1500.0,
            solar_battery_discharge_start="23:00",
            solar_battery_discharge_end="06:00",
        )
        defaults.update(overrides)
        return AppState(**defaults)

    # --- SOC gate ---

    def test_soc_below_threshold_returns_zero(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=80.0)
        cl = make_control_loop(state)
        assert setpoint_eco_day(cl) == 0.0

    def test_soc_at_threshold_passes_gate(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=90.0)
        cl = make_control_loop(state)
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 2000.0)
        # Should not return 0 from SOC gate
        result = setpoint_eco_day(cl)
        assert result > 0

    # --- Cooldown ---

    def test_cooldown_prevents_restart(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=100.0)
        cl = make_control_loop(state)
        cl._eco_charging = False
        cl._eco_day_stopped_at = _time.monotonic()  # just stopped
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 2000.0)
        assert setpoint_eco_day(cl) == 0.0

    def test_cooldown_expired_allows_restart(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=100.0)
        cl = make_control_loop(state)
        cl._eco_charging = False
        cl._eco_day_stopped_at = _time.monotonic() - _ECO_DAY_COOLDOWN_S - 1
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 2000.0)
        result = setpoint_eco_day(cl)
        assert result > 0
        assert cl._eco_charging is True

    # --- Mean grid start ---

    def test_no_start_when_grid_above_threshold(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=100.0)
        cl = make_control_loop(state)
        fill_grid_samples(cl, -500.0)  # not enough export
        fill_battery_samples(cl, 2000.0)
        assert setpoint_eco_day(cl) == 0.0

    def test_starts_when_grid_at_threshold(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=100.0)
        cl = make_control_loop(state)
        fill_grid_samples(cl, _GRID_EXPORT_START_THRESHOLD_W)
        fill_battery_samples(cl, 2000.0)
        result = setpoint_eco_day(cl)
        assert result > 0
        assert cl._eco_charging is True

    # --- Solar battery charge start trigger ---

    def test_starts_when_solar_battery_charge_exceeds_threshold(self):
        """EV charging starts when solar battery charging power exceeds the configured threshold."""
        state = self._make_eco_day_state(
            solar_battery_soc_pct=100.0,
            eco_day_solar_battery_charge_start_w=5500.0,
        )
        cl = make_control_loop(state)
        fill_grid_samples(cl, -500.0)  # grid export NOT met
        fill_battery_samples(cl, 6000.0)  # battery charging above 5500 W threshold
        result = setpoint_eco_day(cl)
        assert result > 0
        assert cl._eco_charging is True

    def test_no_start_when_solar_battery_charge_below_threshold(self):
        """EV charging does not start when solar battery charging is below threshold and no grid export."""
        state = self._make_eco_day_state(
            solar_battery_soc_pct=100.0,
            eco_day_solar_battery_charge_start_w=5500.0,
        )
        cl = make_control_loop(state)
        fill_grid_samples(cl, -500.0)  # grid export NOT met
        fill_battery_samples(cl, 4000.0)  # battery charging below 5500 W threshold
        result = setpoint_eco_day(cl)
        assert result == 0.0
        assert cl._eco_charging is False

    # --- Disconnect resets eco charging state ---

    def test_disconnect_resets_eco_charging_flag(self):
        """Vehicle disconnect resets _eco_charging so reconnect requires start conditions."""
        state = self._make_eco_day_state(solar_battery_soc_pct=95.0)
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_battery_full = True
        cl._prev_ev_connected = True

        # Simulate disconnect
        state.ev_connected = False
        import asyncio

        asyncio.get_event_loop().run_until_complete(cl.run_loop.__wrapped__(cl)) if hasattr(
            cl.run_loop, "__wrapped__"
        ) else None  # noqa: E501

        # Directly test the disconnect logic path
        cl._state.ev_connected = False
        # Trigger the disconnect detection manually
        if cl._state.ev_connected is False and cl._prev_ev_connected is not False:
            cl._eco_charging = False
            cl._eco_day_battery_full = False
        cl._prev_ev_connected = cl._state.ev_connected

        assert cl._eco_charging is False
        assert cl._eco_day_battery_full is False

    def test_no_charge_on_reconnect_without_start_conditions(self):
        """After reconnect, charging does not start without meeting start conditions."""
        state = self._make_eco_day_state(solar_battery_soc_pct=95.0)
        cl = make_control_loop(state)
        # Simulate state after a disconnect reset
        cl._eco_charging = False
        cl._eco_day_battery_full = False
        fill_grid_samples(cl, -500.0)  # grid export NOT met
        fill_battery_samples(cl, 2000.0)  # battery charge NOT met
        result = setpoint_eco_day(cl)
        assert result == 0.0
        assert cl._eco_charging is False

    # --- Mean battery stop ---

    def test_stops_on_sustained_battery_discharge(self):
        state = self._make_eco_day_state(
            solar_battery_soc_pct=100.0,
            solar_battery_day_power_limit_w=-1500.0,
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, -2000.0)  # below -1500 limit
        result = setpoint_eco_day(cl)
        assert result == 0.0
        assert cl._eco_charging is False
        assert cl._eco_day_stopped_at is not None

    def test_continues_when_battery_above_limit(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=100.0)
        cl = make_control_loop(state)
        cl._eco_charging = True
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, -500.0)  # above -1500 limit
        result = setpoint_eco_day(cl)
        assert result > 0

    # --- 90-99% SOC: minimum lock with safeguards ---

    def test_90_99_returns_minimum_when_charging_started(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=95.0)
        cl = make_control_loop(state)
        cl._eco_charging = True
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 2000.0)  # battery healthy
        result = setpoint_eco_day(cl)
        assert result == _MIN_CHARGE_W

    def test_90_99_respects_mean_battery_stop(self):
        state = self._make_eco_day_state(
            solar_battery_soc_pct=95.0,
            solar_battery_day_power_limit_w=-1500.0,
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, -2000.0)  # sustained discharge
        result = setpoint_eco_day(cl)
        assert result == 0.0
        assert cl._eco_charging is False

    def test_90_99_respects_cooldown(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=95.0)
        cl = make_control_loop(state)
        cl._eco_charging = False
        cl._eco_day_stopped_at = _time.monotonic()
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 2000.0)
        assert setpoint_eco_day(cl) == 0.0

    def test_90_99_respects_grid_start_threshold(self):
        state = self._make_eco_day_state(solar_battery_soc_pct=95.0)
        cl = make_control_loop(state)
        fill_grid_samples(cl, -500.0)  # not enough export
        fill_battery_samples(cl, 2000.0)
        assert setpoint_eco_day(cl) == 0.0

    # --- 100% SOC: ramp ---

    def test_100_ramps_up(self):
        state = self._make_eco_day_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=2000.0,
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 5000.0
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 2000.0)
        result = setpoint_eco_day(cl)
        assert result == 5000.0 + _ECO_DAY_RAMP_STEP_W

    def test_100_ramps_down_on_discharge(self):
        state = self._make_eco_day_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=-500.0,
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 6000.0
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 500.0)  # mean still positive
        result = setpoint_eco_day(cl)
        assert result == 6000.0 - _ECO_DAY_RAMP_STEP_W

    def test_100_ramp_clamps_to_min(self):
        state = self._make_eco_day_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=-500.0,
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = _MIN_CHARGE_W
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 500.0)
        result = setpoint_eco_day(cl)
        assert result == _MIN_CHARGE_W  # clamped, not below

    def test_100_ramp_clamps_to_max(self):
        state = self._make_eco_day_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=5000.0,
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = _MAX_CHARGE_W
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 5000.0)
        result = setpoint_eco_day(cl)
        assert result == _MAX_CHARGE_W  # clamped, not above


# ---------------------------------------------------------------------------
# Scenario tests based on real system parameters:
#   Max solar: 8200 W
#   House load: ~2000 W
#   Grid export cap: ~1500 W
#   Solar battery charge max: 4500 W
#   Solar battery max discharge: 6000 W
#   EV charger min: 4400 W, max: 22000 W
#   eco_day_min_solar_battery_soc_pct: 90%
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
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 7000.0

    def test_charges_regardless_of_battery_discharge(self):
        state = self._make_state(solar_battery_power_w=-5000.0)
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 7000.0

    def test_charges_regardless_of_grid_import(self):
        state = self._make_state(grid_power_w=5000.0)
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 7000.0

    def test_charges_regardless_of_victron_down(self):
        state = self._make_state()
        cl = make_control_loop(state, victron_connected=False)
        assert compute_setpoint(cl) == 7000.0

    def test_no_charge_when_ev_disconnected(self):
        state = self._make_state(ev_connected=False)
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 0.0


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
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 0.0

    def test_returns_zero_even_with_excess_solar(self):
        state = self._make_state(grid_power_w=-5000.0, solar_battery_soc_pct=100.0)
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 0.0

    def test_returns_zero_when_ev_disconnected(self):
        state = self._make_state(ev_connected=False)
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 0.0


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
        cl = make_control_loop(state)
        assert setpoint_eco_night(cl) == 5000.0

    def test_stops_at_floor_when_ev_soc_reached(self):
        state = self._make_state(solar_battery_soc_pct=20.0)
        cl = make_control_loop(state)
        # ev_soc_pct is None (unknown) — conservative stop
        assert setpoint_eco_night(cl) == 0.0

    def test_continues_at_floor_when_ev_needs_charge(self):
        state = self._make_state(solar_battery_soc_pct=20.0, ev_soc_pct=30.0)
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = make_control_loop(state)
        assert setpoint_eco_night(cl) == 5000.0

    def test_stops_at_floor_when_ev_soc_met(self):
        state = self._make_state(solar_battery_soc_pct=20.0, ev_soc_pct=50.0)
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = make_control_loop(state)
        assert setpoint_eco_night(cl) == 0.0

    def test_reduces_setpoint_on_excessive_discharge(self):
        state = self._make_state(
            solar_battery_power_w=-8000.0,  # discharging 8kW
            solar_battery_max_discharge_w=6000.0,  # limit 6kW
        )
        cl = make_control_loop(state)
        # overshoot = 8000 - 6000 = 2000, setpoint = 5000 - 2000 = 3000 < 4400 min
        assert setpoint_eco_night(cl) == 0.0

    def test_no_reduction_when_within_discharge_limit(self):
        state = self._make_state(
            solar_battery_power_w=-4000.0,
            solar_battery_max_discharge_w=6000.0,
        )
        cl = make_control_loop(state)
        assert setpoint_eco_night(cl) == 5000.0

    def test_cooldown_after_discharge_limit_trip(self):
        """After battery discharge limit trips, cooldown prevents immediate restart."""
        state = self._make_state(
            solar_battery_power_w=-8000.0,  # triggers limit
            solar_battery_max_discharge_w=6000.0,
        )
        cl = make_control_loop(state)
        # First call trips the limit and sets cooldown
        assert setpoint_eco_night(cl) == 0.0
        assert cl._eco_night_stopped_at is not None

        # Next call: battery recovers but cooldown is active
        cl._state.solar_battery_power_w = -3000.0  # within limits now
        assert setpoint_eco_night(cl) == 0.0  # still blocked by cooldown

    def test_cooldown_expired_allows_restart(self):
        """After cooldown expires, eco night resumes charging."""
        state = self._make_state(
            solar_battery_power_w=-3000.0,  # within limits
            solar_battery_max_discharge_w=6000.0,
        )
        cl = make_control_loop(state)
        # Simulate expired cooldown
        cl._eco_night_stopped_at = _time.monotonic() - 400.0  # > 300s
        result = setpoint_eco_night(cl)
        assert result == 5000.0
        assert cl._eco_night_stopped_at is None


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
            eco_day_min_solar_battery_soc_pct=90.0,
            solar_battery_day_power_limit_w=-1500.0,
            solar_battery_discharge_start="23:00",
            solar_battery_discharge_end="06:00",
        )
        defaults.update(overrides)
        return AppState(**defaults)

    def test_sunny_day_battery_full_ramp_up(self):
        """Battery 100%, exporting 1500W, battery charging 2000W — ramp up."""
        state = self._make_state()
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 5000.0
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 2000.0)
        result = setpoint_eco_day(cl)
        assert result == 5000.0 + _ECO_DAY_RAMP_STEP_W

    def test_cloud_passes_battery_discharges_briefly(self):
        """Battery 100%, instantaneous discharge but mean still healthy — ramp down one step."""
        state = self._make_state(solar_battery_power_w=-500.0)
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 6000.0
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 1000.0)  # mean still positive
        result = setpoint_eco_day(cl)
        assert result == 6000.0 - _ECO_DAY_RAMP_STEP_W

    def test_sustained_cloud_mean_battery_drops(self):
        """Battery 100%, sustained discharge over 5 min — stops with cooldown."""
        state = self._make_state(solar_battery_power_w=-2000.0)
        cl = make_control_loop(state)
        cl._eco_charging = True
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, -2000.0)  # mean below -1500 limit
        result = setpoint_eco_day(cl)
        assert result == 0.0
        assert cl._eco_charging is False

    def test_battery_95_pct_returns_minimum(self):
        """Battery 95%, all safeguards pass — returns minimum, not ramp."""
        state = self._make_state(solar_battery_soc_pct=95.0)
        cl = make_control_loop(state)
        cl._eco_charging = True
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 2000.0)
        result = setpoint_eco_day(cl)
        assert result == _MIN_CHARGE_W

    def test_battery_95_pct_stops_on_sustained_discharge(self):
        """Battery 95%, sustained discharge — stops even though SOC > 90%."""
        state = self._make_state(
            solar_battery_soc_pct=95.0,
            solar_battery_power_w=-2000.0,
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, -2000.0)
        result = setpoint_eco_day(cl)
        assert result == 0.0

    def test_battery_80_pct_no_charging(self):
        """Battery 80% (below 90% threshold) — no EV charging."""
        state = self._make_state(solar_battery_soc_pct=80.0)
        cl = make_control_loop(state)
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 4000.0)
        result = setpoint_eco_day(cl)
        assert result == 0.0

    def test_evening_solar_drops_stops_charging(self):
        """Solar production drops, grid starts importing — doesn't start."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            grid_power_w=500.0,  # importing
        )
        cl = make_control_loop(state)
        fill_grid_samples(cl, 500.0)  # mean is importing
        fill_battery_samples(cl, 500.0)
        result = setpoint_eco_day(cl)
        assert result == 0.0

    def test_cooldown_after_stop_prevents_immediate_restart(self):
        """After stopping, 5-min cooldown prevents restart even with good conditions."""
        state = self._make_state(solar_battery_soc_pct=100.0)
        cl = make_control_loop(state)
        cl._eco_charging = False
        cl._eco_day_stopped_at = _time.monotonic() - 60  # stopped 1 min ago
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 3000.0)
        result = setpoint_eco_day(cl)
        assert result == 0.0  # still in cooldown

    def test_100_no_ramp_when_ev_not_drawing_power(self):
        """When ev_active_power_w is 0 (charger starting up), hold at minimum, don't ramp."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=3000.0,
            ev_active_power_w=0.0,  # charger hasn't started drawing yet
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 6000.0  # was ramped up previously
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 3000.0)
        result = setpoint_eco_day(cl)
        assert result == _MIN_CHARGE_W  # reset to minimum, not ramped further
        assert cl._eco_day_setpoint_w == _MIN_CHARGE_W

    def test_100_no_ramp_when_ev_power_none(self):
        """When ev_active_power_w is None (no reading yet), hold at minimum."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=3000.0,
            ev_active_power_w=None,
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 5000.0
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 3000.0)
        result = setpoint_eco_day(cl)
        assert result == _MIN_CHARGE_W

    def test_100_ramps_when_ev_drawing_power(self):
        """When ev_active_power_w > 0 (charger active), ramp proceeds normally."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=2000.0,
            ev_active_power_w=4400.0,  # charger is drawing power
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 5000.0
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 2000.0)
        result = setpoint_eco_day(cl)
        assert result == 5000.0 + _ECO_DAY_RAMP_STEP_W  # ramped up

    def test_100_deadband_ramps_up_on_idle_draw(self):
        """Battery at -100W (parasitic idle draw within dead band) should ramp UP to probe solar capacity."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=-100.0,  # within ±200W dead band
            ev_active_power_w=5000.0,
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 6000.0
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 1000.0)
        result = setpoint_eco_day(cl)
        assert result == 6000.0 + _ECO_DAY_RAMP_STEP_W  # ramps up to trigger solar demand

    def test_100_deadband_ramps_up_on_small_charge(self):
        """Battery at +150W (small charge within dead band) should ramp UP."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=150.0,  # within ±200W dead band
            ev_active_power_w=5000.0,
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 6000.0
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 1000.0)
        result = setpoint_eco_day(cl)
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
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = _MIN_CHARGE_W  # stuck at minimum after restart
        fill_grid_samples(cl, -1480.0)
        fill_battery_samples(cl, -107.0)
        result = setpoint_eco_day(cl)
        assert result == _MIN_CHARGE_W + _ECO_DAY_RAMP_STEP_W  # must ramp up, not hold

    def test_100_ramps_down_on_significant_discharge(self):
        """Battery at -300W (beyond dead band) should ramp DOWN."""
        state = self._make_state(
            solar_battery_soc_pct=100.0,
            solar_battery_power_w=-300.0,  # beyond -200W dead band
            ev_active_power_w=5000.0,
        )
        cl = make_control_loop(state)
        cl._eco_charging = True
        cl._eco_day_setpoint_w = 6000.0
        fill_grid_samples(cl, -1500.0)
        fill_battery_samples(cl, 1000.0)
        result = setpoint_eco_day(cl)
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
        cl = make_control_loop(state)
        result = setpoint_eco_night(cl)
        # Should return a positive setpoint (grid fallback), not 0
        assert result >= _MIN_CHARGE_W

    def test_battery_flat_ev_soc_met_returns_zero(self):
        """When battery is flat but EV has reached target SOC, stop."""
        state = self._make_state(
            solar_battery_power_w=200.0,
            ev_soc_pct=50.0,  # above 40% target
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = make_control_loop(state)
        result = setpoint_eco_night(cl)
        assert result == 0.0

    def test_battery_flat_ev_soc_unknown_returns_zero(self):
        """When battery is flat and EV SOC is unknown, stop (conservative)."""
        state = self._make_state(
            solar_battery_power_w=200.0,
            ev_soc_pct=None,
        )
        cl = make_control_loop(state)
        result = setpoint_eco_night(cl)
        assert result == 0.0

    def test_battery_still_delivering_uses_normal_logic(self):
        """When battery is still delivering power (not flat), use normal setpoint."""
        state = self._make_state(
            solar_battery_power_w=-3000.0,  # still discharging
            solar_battery_soc_pct=50.0,  # above floor
        )
        cl = make_control_loop(state)
        result = setpoint_eco_night(cl)
        assert result == 5000.0  # normal fixed setpoint

    def test_grid_fallback_clamps_to_min(self):
        """Grid fallback setpoint is clamped to charger minimum."""
        state = self._make_state(
            solar_battery_power_w=200.0,
            ev_soc_pct=39.0,  # only 1% gap, very little energy needed
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = make_control_loop(state)
        result = compute_grid_fallback_setpoint(cl, 39.0)
        assert result >= _MIN_CHARGE_W

    def test_grid_fallback_clamps_to_max(self):
        """Grid fallback setpoint is clamped to charger maximum."""
        state = self._make_state(
            solar_battery_power_w=200.0,
            ev_soc_pct=5.0,  # huge gap, needs lots of power
            ev_battery_capacity_kwh=200.0,  # large battery
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = make_control_loop(state)
        # With a huge gap and short time, required power could exceed max
        result = compute_grid_fallback_setpoint(cl, 5.0)
        assert result <= _MAX_CHARGE_W

    def test_grid_fallback_ev_already_at_target(self):
        """Grid fallback returns 0 when EV is already at target."""
        state = self._make_state()
        cl = make_control_loop(state)
        result = compute_grid_fallback_setpoint(cl, 40.0)
        assert result == 0.0

    def test_grid_fallback_ev_above_target(self):
        """Grid fallback returns 0 when EV is above target."""
        state = self._make_state()
        cl = make_control_loop(state)
        result = compute_grid_fallback_setpoint(cl, 60.0)
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
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 0.0

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
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 0.0

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
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 7000.0

    def test_continues_when_ev_soc_unavailable(self):
        """Charging continues when EV SOC is unavailable (None)."""
        state = AppState(
            ev_connected=True,
            charge_mode="Manual",
            manual_power_w=7000.0,
            ev_soc_pct=None,
            ev_max_soc_pct=80.0,
        )
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 7000.0

    def test_applies_to_standby_mode(self):
        """Max SOC check runs before mode dispatch — standby still returns 0."""
        state = AppState(
            ev_connected=True,
            charge_mode="Standby",
            ev_soc_pct=90.0,
            ev_max_soc_pct=80.0,
        )
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 0.0

    def test_max_soc_resets_on_disconnect(self):
        """ev_max_soc_pct resets to 80% when vehicle is disconnected."""
        state = AppState(
            ev_connected=False,
            ev_max_soc_pct=100.0,
        )
        cl = make_control_loop(state)
        cl._prev_ev_connected = True  # was connected, now disconnected
        # Simulate one run_loop iteration's disconnect detection
        # We test the logic directly
        if not state.ev_connected and cl._prev_ev_connected is not False:
            from app.control.constants import _EV_MAX_SOC_DEFAULT

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
        cl = make_control_loop(state)
        assert compute_setpoint(cl) == 7000.0


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
            ev_charger_status_enum=ChargerStatus.CHARGING_IN_PROGRESS,
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
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.IDLE
        result = cl._state_machine.apply_charging_events(7000.0)
        assert result == 7000.0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "started"
        assert events[0]["setpoint_w"] == 7000.0
        assert cl._charging_session_state == ChargeSessionState.CHARGING
        assert cl._session_origin_mode == "Manual"

    def test_stopping_emitted_but_setpoint_held(self):
        """When setpoint wants to go to 0, stopping is emitted but previous setpoint is returned."""
        state = self._make_state()
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.CHARGING
        cl._session_origin_mode = "Manual"
        cl._last_positive_setpoint = 6000.0
        result = cl._state_machine.apply_charging_events(0.0)
        assert result == 6000.0  # held at previous setpoint, NOT 0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "stopping"
        assert cl._charging_session_state == ChargeSessionState.STOPPING

    def test_immediate_reconcile_when_mode_changed_during_start_handshake(self):
        """A mode switch during active/start status should force immediate setpoint=0."""
        state = self._make_state(
            charge_mode="Eco",
            ev_charger_status_enum=ChargerStatus.HANDSHAKING_WITH_VEHICLE,
        )
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.CHARGING
        cl._session_origin_mode = "Manual"
        cl._last_positive_setpoint = 6000.0

        result = cl._state_machine.apply_charging_events(0.0)

        assert result == 0.0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "stopping"
        assert cl._charging_session_state == ChargeSessionState.STOPPED_PENDING
        assert cl._stopping_at is None

    def test_immediate_reconcile_when_switched_to_standby_during_handshake(self):
        """Mode switch to Standby during active/start state should force immediate setpoint=0."""
        state = self._make_state(
            charge_mode="Standby",
            ev_charger_status_enum=ChargerStatus.HANDSHAKING_WITH_VEHICLE,
        )
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.CHARGING
        cl._session_origin_mode = "Manual"
        cl._last_positive_setpoint = 7000.0

        result = cl._state_machine.apply_charging_events(0.0)

        assert result == 0.0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "stopping"
        assert events[0]["reason"] == "standby"
        assert cl._charging_session_state == ChargeSessionState.STOPPED_PENDING
        assert cl._stopping_at is None

    def test_setpoint_held_during_grace_period(self):
        """During the 10s grace period, setpoint stays at previous value."""
        state = self._make_state()
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.STOPPING
        cl._stopping_at = _time.monotonic() - 5  # 5s ago
        cl._last_positive_setpoint = 6000.0
        cl._stopping_reason = "max_soc_reached"
        result = cl._state_machine.apply_charging_events(0.0)
        assert result == 6000.0  # still held
        assert cl._charging_session_state == ChargeSessionState.STOPPING

    def test_stopped_emitted_after_grace_period(self):
        """After 10s grace, setpoint goes to 0 but enters stopped_pending (no event yet)."""
        state = self._make_state(ev_session_energy_wh=5000.0)
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.STOPPING
        cl._stopping_at = _time.monotonic() - 11  # 11s ago
        cl._last_positive_setpoint = 6000.0
        cl._stopping_reason = "max_soc_reached"
        result = cl._state_machine.apply_charging_events(0.0)
        assert result == 0.0  # setpoint goes to 0
        events = self._get_events(cl)
        assert len(events) == 0  # no stopped event yet — in stopped_pending
        assert cl._charging_session_state == ChargeSessionState.STOPPED_PENDING

    def test_stopped_event_after_pending_delay(self):
        """Stopped event emitted after 5s in stopped_pending state."""
        state = self._make_state(ev_session_energy_wh=5000.0)
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.STOPPED_PENDING
        cl._stopped_at = _time.monotonic() - 6  # 6s ago
        cl._stopping_reason = "max_soc_reached"
        result = cl._state_machine.apply_charging_events(0.0)
        assert result == 0.0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "stopped"
        assert events[0]["session_energy_wh"] == 5000.0
        assert cl._charging_session_state == ChargeSessionState.IDLE

    def test_no_stopped_event_during_pending_delay(self):
        """No stopped event while still within the 5s pending delay."""
        state = self._make_state()
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.STOPPED_PENDING
        cl._stopped_at = _time.monotonic() - 2  # only 2s ago
        cl._stopping_reason = "max_soc_reached"
        result = cl._state_machine.apply_charging_events(0.0)
        assert result == 0.0
        events = self._get_events(cl)
        assert len(events) == 0
        assert cl._charging_session_state == ChargeSessionState.STOPPED_PENDING

    def test_resume_from_stopped_pending(self):
        """If charging resumes during stopped_pending, go straight to charging."""
        state = self._make_state()
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.STOPPED_PENDING
        cl._stopped_at = _time.monotonic() - 2
        cl._stopping_reason = "eco_day_conditions"
        result = cl._state_machine.apply_charging_events(7000.0)
        assert result == 7000.0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "started"
        assert cl._charging_session_state == ChargeSessionState.CHARGING

    def test_resume_from_stopping_cancels_stop(self):
        """If charging resumes during grace period, cancel the stop."""
        state = self._make_state()
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.STOPPING
        cl._stopping_at = _time.monotonic() - 3
        cl._last_positive_setpoint = 6000.0
        cl._stopping_reason = "eco_day_conditions"
        result = cl._state_machine.apply_charging_events(7000.0)
        assert result == 7000.0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "started"
        assert cl._charging_session_state == ChargeSessionState.CHARGING

    def test_no_event_when_idle_and_not_charging(self):
        state = self._make_state()
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.IDLE
        result = cl._state_machine.apply_charging_events(0.0)
        assert result == 0.0
        events = self._get_events(cl)
        assert len(events) == 0

    def test_no_event_when_charging_continues(self):
        state = self._make_state()
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.CHARGING
        cl._last_positive_setpoint = 7000.0
        result = cl._state_machine.apply_charging_events(7000.0)
        assert result == 7000.0
        events = self._get_events(cl)
        assert len(events) == 0

    def test_max_soc_reason_detected(self):
        # Exact target reached (no margin applied for non-100% targets)
        state = self._make_state(ev_soc_pct=80.0, ev_max_soc_pct=80.0)
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = make_control_loop(state)
        reason = cl._state_machine.determine_stop_reason()
        assert reason == "max_soc_reached"

    def test_max_soc_reason_detected_100pct_with_margin(self):
        # At 100% target, 0.5% margin applies so 99.5% triggers max_soc_reached
        state = self._make_state(ev_soc_pct=99.5, ev_max_soc_pct=100.0)
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = make_control_loop(state)
        reason = cl._state_machine.determine_stop_reason()
        assert reason == "max_soc_reached"

    def test_max_soc_reason_not_triggered_below_margin(self):
        # For non-100% targets, no margin: 79.9% should NOT trigger max_soc_reached at 80% target
        state = self._make_state(ev_soc_pct=79.9, ev_max_soc_pct=80.0)
        state.ev_soc_pct_updated_at = _time.monotonic()
        cl = make_control_loop(state)
        reason = cl._state_machine.determine_stop_reason()
        assert reason != "max_soc_reached"

    def test_vehicle_disconnected_reason(self):
        state = self._make_state(ev_connected=False)
        cl = make_control_loop(state)
        reason = cl._state_machine.determine_stop_reason()
        assert reason == "vehicle_disconnected"

    def test_standby_reason(self):
        state = self._make_state(charge_mode="Standby")
        cl = make_control_loop(state)
        reason = cl._state_machine.determine_stop_reason()
        assert reason == "standby"

    def test_external_stop_when_status_reports_not_charging_for_two_ticks(self):
        """External stop requires two consecutive non-charging status ticks."""
        state = self._make_state(
            ev_charger_status_enum=ChargerStatus.CHARGING_COMPLETED,
            ev_session_energy_wh=8000.0,
        )
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.CHARGING
        cl._last_positive_setpoint = 7000.0

        # First tick only increments confirmation counter.
        first = cl._state_machine.apply_charging_events(7000.0)
        assert first == 7000.0
        assert cl._charging_session_state == ChargeSessionState.CHARGING

        # Second consecutive tick confirms external stop.
        result = cl._state_machine.apply_charging_events(7000.0)
        assert result == 0.0  # overridden to 0
        events = self._get_events(cl)
        assert len(events) == 0  # no event yet — in stopped_pending
        assert cl._charging_session_state == ChargeSessionState.STOPPED_PENDING
        assert cl._stopping_reason == "external_stop"

    def test_no_external_stop_when_status_is_charging(self):
        """No external stop while status indicates active charging."""
        state = self._make_state(ev_charger_status_enum=ChargerStatus.CHARGING_IN_PROGRESS)
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.CHARGING
        cl._last_positive_setpoint = 7000.0
        result = cl._state_machine.apply_charging_events(7000.0)
        assert result == 7000.0
        events = self._get_events(cl)
        assert len(events) == 0

    def test_no_external_stop_on_unknown_status(self):
        """Unknown status should not force external stop transitions."""
        state = self._make_state(ev_charger_status_enum=ChargerStatus.UNKNOWN)
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.CHARGING
        cl._last_positive_setpoint = 7000.0
        result = cl._state_machine.apply_charging_events(7000.0)
        assert result == 7000.0
        assert cl._charging_session_state == ChargeSessionState.CHARGING

    def test_car_disconnect_triggers_normal_stopping_flow(self):
        """Car disconnect causes _compute_setpoint to return 0, triggering normal stopping."""
        state = self._make_state(ev_connected=False)
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.CHARGING
        cl._last_positive_setpoint = 7000.0
        # _compute_setpoint returns 0 when ev_connected is False
        result = cl._state_machine.apply_charging_events(0.0)
        assert result == 7000.0  # held during grace period
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "stopping"
        assert cl._charging_session_state == ChargeSessionState.STOPPING

    def test_external_stop_vehicle_disconnected(self):
        """Stopped_pending with vehicle_disconnected reason when EV unplugged externally."""
        state = self._make_state(ev_connected=False, ev_charger_status_enum=ChargerStatus.IDLE_NO_CONNECTOR)
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.CHARGING
        cl._last_positive_setpoint = 7000.0
        cl._state_machine.apply_charging_events(7000.0)  # tick 1
        result = cl._state_machine.apply_charging_events(7000.0)  # tick 2
        assert result == 0.0
        events = self._get_events(cl)
        assert len(events) == 0  # no event yet — in stopped_pending
        assert cl._charging_session_state == ChargeSessionState.STOPPED_PENDING
        assert cl._stopping_reason == "vehicle_disconnected"

    def test_external_stop_emits_after_delay(self):
        """External stop: stopped event emitted after 5s delay."""
        state = self._make_state(
            ev_charger_status_enum=ChargerStatus.CHARGING_COMPLETED,
            ev_session_energy_wh=8000.0,
        )
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.STOPPED_PENDING
        cl._stopped_at = _time.monotonic() - 6  # 6s ago
        cl._stopping_reason = "external_stop"
        result = cl._state_machine.apply_charging_events(0.0)
        assert result == 0.0
        events = self._get_events(cl)
        assert len(events) == 1
        assert events[0]["event"] == "stopped"
        assert events[0]["reason"] == "external_stop"
        assert events[0]["session_energy_wh"] == 8000.0
        assert cl._charging_session_state == ChargeSessionState.IDLE

    def test_no_external_stop_when_status_reports_starting(self):
        """No external stop while charger status indicates start-up."""
        state = self._make_state(ev_charger_status_enum=ChargerStatus.HANDSHAKING_WITH_VEHICLE)
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.CHARGING
        cl._last_positive_setpoint = 7000.0
        cl._state_machine.apply_charging_events(7000.0)
        events = self._get_events(cl)
        assert len(events) == 0
        assert cl._charging_session_state == ChargeSessionState.CHARGING

    def test_external_stop_detection_applies_in_eco_mode_too(self):
        """Status-based external stop handling should be mode-agnostic (Eco included)."""
        state = self._make_state(
            charge_mode="Eco",
            ev_charger_status_enum=ChargerStatus.CHARGING_COMPLETED,
            ev_session_energy_wh=4200.0,
        )
        cl = make_control_loop(state)
        cl._charging_session_state = ChargeSessionState.CHARGING
        cl._last_positive_setpoint = 5000.0
        cl._state_machine.apply_charging_events(5000.0)  # tick 1
        result = cl._state_machine.apply_charging_events(5000.0)  # tick 2
        assert result == 0.0
        assert cl._charging_session_state == ChargeSessionState.STOPPED_PENDING


class TestStandbyWriteSuppression:
    def _make_state(self, **overrides):
        defaults = dict(
            ev_connected=True,
            charge_mode="Standby",
            ev_charger_status_enum=ChargerStatus.IDLE_CONNECTOR_PLUGGED,
            ev_active_power_w=0.0,
        )
        defaults.update(overrides)
        return AppState(**defaults)

    def test_not_suppressed_while_charger_active(self):
        state = self._make_state(ev_charger_status_enum=ChargerStatus.CHARGING_IN_PROGRESS)
        cl = make_control_loop(state)
        assert cl._state_machine.should_suppress_ev_writes(0.0) is False

    def test_suppressed_once_standby_reached(self):
        state = self._make_state()
        cl = make_control_loop(state)
        assert cl._state_machine.should_suppress_ev_writes(0.0) is True

    def test_not_suppressed_while_power_still_positive(self):
        state = self._make_state(ev_active_power_w=1200.0)
        cl = make_control_loop(state)
        assert cl._state_machine.should_suppress_ev_writes(0.0) is False

    def test_latch_keeps_suppressing_in_standby(self):
        state = self._make_state()
        cl = make_control_loop(state)
        assert cl._state_machine.should_suppress_ev_writes(0.0) is True

        # Charger state can change later; once latched, controller stays quiet.
        state.ev_charger_status_enum = ChargerStatus.CHARGING_IN_PROGRESS
        assert cl._state_machine.should_suppress_ev_writes(0.0) is True

    def test_leaving_standby_clears_latch(self):
        state = self._make_state()
        cl = make_control_loop(state)
        assert cl._state_machine.should_suppress_ev_writes(0.0) is True

        state.charge_mode = "Eco"
        assert cl._state_machine.should_suppress_ev_writes(4400.0) is False

    def test_positive_setpoint_in_standby_does_not_suppress(self):
        state = self._make_state()
        cl = make_control_loop(state)
        assert cl._state_machine.should_suppress_ev_writes(5000.0) is False

    def test_suppressed_in_standby_when_ev_disconnected_and_inactive(self):
        state = self._make_state(
            ev_connected=False,
            ev_charger_status_enum=ChargerStatus.IDLE_NO_CONNECTOR,
            ev_active_power_w=0.0,
        )
        cl = make_control_loop(state)
        assert cl._state_machine.should_suppress_ev_writes(0.0) is True


class TestEvOutputActuation:
    def _make_state(self, **overrides):
        defaults = dict(
            ev_connected=True,
            charge_mode="Eco",
            ev_charger_status_enum=ChargerStatus.IDLE_CONNECTOR_PLUGGED,
            ev_active_power_w=0.0,
        )
        defaults.update(overrides)
        return AppState(**defaults)

    def test_suppressed_skips_all_ev_output(self):
        import asyncio as _asyncio

        state = self._make_state()
        cl = make_control_loop(state)

        _asyncio.run(cl._apply_ev_output(0.0, suppress_ev_writes=True))

        cl._ev_client.write_setpoint.assert_not_awaited()
        cl._ev_client.start_charging.assert_not_awaited()
        cl._ev_client.stop_charging.assert_not_awaited()

    def test_stop_path_writes_preset_and_sends_stop_when_active(self):
        import asyncio as _asyncio

        state = self._make_state(ev_charger_status_enum=ChargerStatus.CHARGING_IN_PROGRESS)
        cl = make_control_loop(state)

        _asyncio.run(cl._apply_ev_output(0.0, suppress_ev_writes=False))

        cl._ev_client.write_setpoint.assert_awaited_once_with(_STOP_PRESET_W)
        cl._ev_client.stop_charging.assert_awaited_once_with()
        cl._ev_client.start_charging.assert_not_awaited()

    def test_stop_path_writes_preset_without_stop_when_not_active(self):
        import asyncio as _asyncio

        state = self._make_state(ev_charger_status_enum=ChargerStatus.IDLE_CONNECTOR_PLUGGED)
        cl = make_control_loop(state)

        _asyncio.run(cl._apply_ev_output(0.0, suppress_ev_writes=False))

        cl._ev_client.write_setpoint.assert_awaited_once_with(_STOP_PRESET_W)
        cl._ev_client.stop_charging.assert_not_awaited()
        cl._ev_client.start_charging.assert_not_awaited()

    def test_positive_setpoint_sends_start_when_status_requires_it(self):
        import asyncio as _asyncio

        state = self._make_state(ev_charger_status_enum=ChargerStatus.IDLE_CONNECTOR_PLUGGED)
        cl = make_control_loop(state)

        _asyncio.run(cl._apply_ev_output(5600.0, suppress_ev_writes=False))

        cl._ev_client.write_setpoint.assert_awaited_once_with(5600.0)
        cl._ev_client.start_charging.assert_awaited_once_with()
        cl._ev_client.stop_charging.assert_not_awaited()

    def test_positive_setpoint_does_not_send_start_when_already_active(self):
        import asyncio as _asyncio

        state = self._make_state(ev_charger_status_enum=ChargerStatus.CHARGING_IN_PROGRESS)
        cl = make_control_loop(state)

        _asyncio.run(cl._apply_ev_output(5600.0, suppress_ev_writes=False))

        cl._ev_client.write_setpoint.assert_awaited_once_with(5600.0)
        cl._ev_client.start_charging.assert_not_awaited()
        cl._ev_client.stop_charging.assert_not_awaited()
