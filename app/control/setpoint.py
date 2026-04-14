"""Setpoint calculation helpers for the control loop."""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime
from typing import TYPE_CHECKING

from app.control.constants import (
    _ECO_DAY_COOLDOWN_S,
    _ECO_DAY_RAMP_STEP_W,
    _EV_MAX_SOC_MARGIN_PCT,
    _EV_SOC_STALE_S,
    _GRID_EXPORT_START_THRESHOLD_W,
    _MAX_CHARGE_W,
    _MIN_CHARGE_W,
    _RAMP_DEADBAND_W,
)
from app.control.state_machine import ChargeModeState, set_mode_state
from app.control.time_utils import is_within_discharge_window, parse_hhmm, validate_hhmm

if TYPE_CHECKING:
    from app.control.loop import ControlLoop

logger = logging.getLogger(__name__)


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp *value* to the range [min_val, max_val]."""
    return max(min_val, min(value, max_val))


def get_ev_soc(loop: ControlLoop) -> float | None:
    """Return ev_soc_pct if fresh, else None."""
    state = loop._state
    if state.ev_soc_pct is None or state.ev_soc_pct_updated_at is None:
        return None
    if (_time.monotonic() - state.ev_soc_pct_updated_at) > _EV_SOC_STALE_S:
        return None
    return state.ev_soc_pct


def record_samples(loop: ControlLoop) -> None:
    """Record current grid and battery power readings into rolling buffers."""
    now = _time.monotonic()
    if loop._state.grid_power_w is not None:
        loop._grid_power_samples.append((now, loop._state.grid_power_w))
    if loop._state.solar_battery_power_w is not None:
        loop._battery_power_samples.append((now, loop._state.solar_battery_power_w))
    prune_samples(loop)


def prune_samples(loop: ControlLoop) -> None:
    """Remove samples older than the configured window."""
    cutoff = _time.monotonic() - (loop._state.eco_mean_window_minutes * 60)
    loop._grid_power_samples = [(t, v) for t, v in loop._grid_power_samples if t >= cutoff]
    loop._battery_power_samples = [(t, v) for t, v in loop._battery_power_samples if t >= cutoff]


def mean_grid_power(loop: ControlLoop) -> float | None:
    """Return the mean grid power over the rolling window."""
    if not loop._grid_power_samples:
        return None
    return sum(v for _, v in loop._grid_power_samples) / len(loop._grid_power_samples)


def mean_battery_power(loop: ControlLoop) -> float | None:
    """Return the mean battery power over the rolling window."""
    if not loop._battery_power_samples:
        return None
    return sum(v for _, v in loop._battery_power_samples) / len(loop._battery_power_samples)


def compute_setpoint(loop: ControlLoop) -> float:
    """Compute the current charge-power setpoint."""
    if not loop._state.ev_connected:
        set_mode_state(loop, ChargeModeState.NO_VEHICLE)
        return 0.0

    ev_soc = get_ev_soc(loop)
    if ev_soc is not None and ev_soc >= (loop._state.ev_max_soc_pct - _EV_MAX_SOC_MARGIN_PCT):
        set_mode_state(loop, ChargeModeState.MAX_SOC_BLOCKED)
        return 0.0

    mode = loop._state.charge_mode
    if mode == "Manual":
        return setpoint_manual(loop)
    if mode == "Standby":
        return setpoint_standby(loop)

    if not loop._victron_client.connected:
        logger.warning("Eco mode: Victron comms down - pausing EV charging")
        loop._eco_charging = False
        set_mode_state(loop, ChargeModeState.ECO_VICTRON_DOWN)
        return 0.0
    if is_within_discharge_window(loop._state):
        return setpoint_eco_night(loop)
    return setpoint_eco_day(loop)


def setpoint_manual(loop: ControlLoop) -> float:
    """Manual: charge at a fixed user-configured power."""
    set_mode_state(loop, ChargeModeState.MANUAL)
    return clamp(loop._state.manual_power_w, _MIN_CHARGE_W, _MAX_CHARGE_W)


def setpoint_standby(loop: ControlLoop) -> float:
    """Standby: no charging."""
    loop._eco_charging = False
    loop._eco_day_setpoint_w = _MIN_CHARGE_W
    set_mode_state(loop, ChargeModeState.STANDBY)
    return 0.0


def setpoint_eco_night(loop: ControlLoop) -> float:
    """Eco inside discharge window: draw from solar battery at a fixed rate."""
    state = loop._state
    ev_soc = get_ev_soc(loop)

    battery_flat = (
        state.solar_battery_power_w is not None
        and state.solar_battery_power_w > -100.0
        and state.solar_battery_soc_pct is not None
        and state.solar_battery_soc_pct <= state.solar_battery_discharge_floor_pct
    )

    if battery_flat:
        ev_needs_charge = ev_soc is not None and ev_soc < state.ev_min_soc_pct
        if not ev_needs_charge:
            set_mode_state(loop, ChargeModeState.ECO_NIGHT_FLOOR_STOP)
            return 0.0
        set_mode_state(loop, ChargeModeState.ECO_NIGHT_GRID_FALLBACK)
        return compute_grid_fallback_setpoint(loop, ev_soc)

    at_floor = (
        state.solar_battery_soc_pct is not None
        and state.solar_battery_soc_pct <= state.solar_battery_discharge_floor_pct
    )
    if at_floor:
        ev_needs_charge = ev_soc is not None and ev_soc < state.ev_min_soc_pct
        if not ev_needs_charge:
            set_mode_state(loop, ChargeModeState.ECO_NIGHT_FLOOR_STOP)
            return 0.0

    set_mode_state(loop, ChargeModeState.ECO_NIGHT_BATTERY)
    setpoint = clamp(state.solar_battery_max_ev_charge_power_w, _MIN_CHARGE_W, _MAX_CHARGE_W)
    return limit_battery_discharge(loop, setpoint, state.solar_battery_max_discharge_w)


def compute_grid_fallback_setpoint(loop: ControlLoop, ev_soc: float) -> float:
    """Calculate grid power needed to reach ev_min_soc_pct by discharge-window end."""
    state = loop._state

    soc_gap = state.ev_min_soc_pct - ev_soc
    if soc_gap <= 0:
        return 0.0
    energy_needed_kwh = soc_gap / 100.0 * state.ev_battery_capacity_kwh

    if not validate_hhmm(state.solar_battery_discharge_end):
        return _MIN_CHARGE_W

    end_time = parse_hhmm(state.solar_battery_discharge_end)
    now = datetime.now().time()  # noqa: DTZ005

    now_s = now.hour * 3600 + now.minute * 60 + now.second
    end_s = end_time.hour * 3600 + end_time.minute * 60
    remaining_s = end_s - now_s
    if remaining_s <= 0:
        remaining_s += 86400

    if remaining_s < 60:
        return 0.0

    remaining_h = remaining_s / 3600.0
    required_w = (energy_needed_kwh / remaining_h) * 1000.0

    logger.debug(
        "Eco night grid fallback: EV SOC %.0f%% -> %.0f%%, need %.1f kWh in %.1f h, required %.0f W",
        ev_soc,
        state.ev_min_soc_pct,
        energy_needed_kwh,
        remaining_h,
        required_w,
    )

    return clamp(required_w, _MIN_CHARGE_W, _MAX_CHARGE_W)


def setpoint_eco_day(loop: ControlLoop) -> float:
    """Eco outside discharge window: charge from excess solar."""
    state = loop._state

    if state.solar_battery_soc_pct is not None and state.solar_battery_soc_pct < state.eco_day_min_battery_soc_pct:
        if loop._eco_charging:
            logger.info(
                "Eco day: pausing charge (home battery SOC %.0f%% < threshold %.0f%%)",
                state.solar_battery_soc_pct,
                state.eco_day_min_battery_soc_pct,
            )
            loop._eco_charging = False
        set_mode_state(loop, ChargeModeState.ECO_DAY_SOC_GATE)
        return 0.0

    battery_full = state.solar_battery_soc_pct is not None and state.solar_battery_soc_pct >= 98.0
    current_mean_grid = mean_grid_power(loop)
    current_mean_battery = mean_battery_power(loop)

    if not loop._eco_charging and loop._eco_day_stopped_at is not None:
        elapsed = _time.monotonic() - loop._eco_day_stopped_at
        if elapsed < _ECO_DAY_COOLDOWN_S:
            set_mode_state(loop, ChargeModeState.ECO_DAY_COOLDOWN)
            return 0.0

    if not loop._eco_charging:
        if current_mean_grid is not None and current_mean_grid <= _GRID_EXPORT_START_THRESHOLD_W:
            loop._eco_charging = True
            loop._eco_day_setpoint_w = _MIN_CHARGE_W
            loop._eco_day_stopped_at = None
            logger.info(
                "Eco day: starting charge at %.0f W (mean grid=%.0f W)",
                loop._eco_day_setpoint_w,
                current_mean_grid,
            )
        else:
            set_mode_state(loop, ChargeModeState.ECO_DAY_WAITING_FOR_EXPORT)
            return 0.0

    if current_mean_battery is not None and current_mean_battery < state.solar_battery_day_power_limit_w:
        loop._eco_charging = False
        loop._eco_day_stopped_at = _time.monotonic()
        logger.info(
            "Eco day: stopping charge (mean battery=%.0f W, limit=%.0f W), cooldown %.0f s",
            current_mean_battery,
            state.solar_battery_day_power_limit_w,
            _ECO_DAY_COOLDOWN_S,
        )
        set_mode_state(loop, ChargeModeState.ECO_DAY_COOLDOWN)
        return 0.0

    if not battery_full:
        set_mode_state(loop, ChargeModeState.ECO_DAY_MINIMUM)
        return _MIN_CHARGE_W

    ev_power = state.ev_active_power_w
    if ev_power is None or ev_power <= 0:
        loop._eco_day_setpoint_w = _MIN_CHARGE_W
        set_mode_state(loop, ChargeModeState.ECO_DAY_MINIMUM)
        return _MIN_CHARGE_W

    battery_power = state.solar_battery_power_w
    if battery_power is not None and battery_power < -_RAMP_DEADBAND_W:
        loop._eco_day_setpoint_w -= _ECO_DAY_RAMP_STEP_W
    else:
        loop._eco_day_setpoint_w += _ECO_DAY_RAMP_STEP_W

    loop._eco_day_setpoint_w = clamp(loop._eco_day_setpoint_w, _MIN_CHARGE_W, _MAX_CHARGE_W)
    set_mode_state(loop, ChargeModeState.ECO_DAY_RAMPING)
    return loop._eco_day_setpoint_w


def limit_battery_discharge(loop: ControlLoop, setpoint: float, max_discharge_w: float) -> float:
    """Reduce setpoint if home battery discharge exceeds the allowed limit."""
    battery_power = loop._state.solar_battery_power_w
    if battery_power is not None and battery_power < 0:
        overshoot = abs(battery_power) - max_discharge_w
        if overshoot > 0:
            setpoint -= overshoot
            if setpoint < _MIN_CHARGE_W:
                return 0.0
            setpoint = clamp(setpoint, _MIN_CHARGE_W, _MAX_CHARGE_W)
    return setpoint
