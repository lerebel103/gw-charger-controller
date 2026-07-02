"""Charging mode strategy objects and setpoint dispatch."""

import logging
import time as _time
from abc import ABC, abstractmethod

from app.control.constants import (
    _ECO_DAY_COOLDOWN_S,
    _EV_MAX_SOC_MARGIN_PCT,
    _MAX_CHARGE_W,
    _MIN_CHARGE_W,
    _RAMP_DEADBAND_W,
)
from app.control.power_utils import (
    clamp,
    compute_grid_fallback_setpoint,
    get_ev_soc,
    limit_battery_discharge,
    mean_battery_power,
    mean_grid_power,
)
from app.control.protocols import ModeLoopProtocol
from app.control.time_utils import is_within_discharge_window
from app.log_throttle import LogThrottle
from app.state import ChargeModeState

logger = logging.getLogger(__name__)
_throttle = LogThrottle(logger, suppress_seconds=60.0)


class ModeSetpointHandler(ABC):
    """Encapsulated setpoint strategy for one top-level charging mode state."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Diagnostic name of this mode handler."""

    @abstractmethod
    def compute(self, loop: ModeLoopProtocol) -> float:
        """Return the setpoint to apply for this mode handler."""


class NoVehicleModeHandler(ModeSetpointHandler):
    @property
    def name(self) -> str:
        return "no_vehicle"

    def compute(self, loop: ModeLoopProtocol) -> float:
        loop._state_machine.set_mode_state(ChargeModeState.NO_VEHICLE)
        return 0.0


class MaxSocBlockedModeHandler(ModeSetpointHandler):
    @property
    def name(self) -> str:
        return "max_soc_blocked"

    def compute(self, loop: ModeLoopProtocol) -> float:
        loop._state_machine.set_mode_state(ChargeModeState.MAX_SOC_BLOCKED)
        return 0.0


class ManualModeHandler(ModeSetpointHandler):
    @property
    def name(self) -> str:
        return "manual"

    def compute(self, loop: ModeLoopProtocol) -> float:
        loop._state_machine.set_mode_state(ChargeModeState.MANUAL)
        return clamp(loop._state.manual_power_w, _MIN_CHARGE_W, _MAX_CHARGE_W)


class StandbyModeHandler(ModeSetpointHandler):
    @property
    def name(self) -> str:
        return "standby"

    def compute(self, loop: ModeLoopProtocol) -> float:
        loop._eco_charging = False
        loop._eco_day_setpoint_w = _MIN_CHARGE_W
        loop._eco_day_battery_full = False
        loop._state_machine.set_mode_state(ChargeModeState.STANDBY)
        return 0.0


class EcoVictronDownModeHandler(ModeSetpointHandler):
    @property
    def name(self) -> str:
        return "eco_victron_down"

    def compute(self, loop: ModeLoopProtocol) -> float:
        _throttle.warning("eco_victron_down", "Eco mode: Victron comms down - pausing EV charging")
        loop._eco_charging = False
        loop._eco_day_battery_full = False
        loop._state_machine.set_mode_state(ChargeModeState.ECO_VICTRON_DOWN)
        return 0.0


class EcoNightModeHandler(ModeSetpointHandler):
    @property
    def name(self) -> str:
        return "eco_night"

    def compute(self, loop: ModeLoopProtocol) -> float:
        state = loop._state
        ev_soc = get_ev_soc(loop)

        # Cooldown after battery discharge limit trip
        if loop._eco_night_stopped_at is not None:
            elapsed = _time.monotonic() - loop._eco_night_stopped_at
            if elapsed < _ECO_DAY_COOLDOWN_S:
                loop._state_machine.set_mode_state(ChargeModeState.ECO_NIGHT_FLOOR_STOP)
                return 0.0
            loop._eco_night_stopped_at = None

        battery_flat = (
            state.solar_battery_power_w is not None
            and state.solar_battery_power_w > -100.0
            and state.solar_battery_soc_pct is not None
            and state.solar_battery_soc_pct <= state.solar_battery_discharge_floor_pct
        )

        if battery_flat:
            ev_needs_charge = ev_soc is not None and ev_soc < state.ev_min_soc_pct
            if not ev_needs_charge:
                loop._state_machine.set_mode_state(ChargeModeState.ECO_NIGHT_FLOOR_STOP)
                return 0.0
            loop._state_machine.set_mode_state(ChargeModeState.ECO_NIGHT_GRID_FALLBACK)
            return compute_grid_fallback_setpoint(loop, ev_soc)

        at_floor = (
            state.solar_battery_soc_pct is not None
            and state.solar_battery_soc_pct <= state.solar_battery_discharge_floor_pct
        )
        if at_floor:
            ev_needs_charge = ev_soc is not None and ev_soc < state.ev_min_soc_pct
            if not ev_needs_charge:
                loop._state_machine.set_mode_state(ChargeModeState.ECO_NIGHT_FLOOR_STOP)
                return 0.0

        loop._state_machine.set_mode_state(ChargeModeState.ECO_NIGHT_BATTERY)
        setpoint = clamp(state.solar_battery_max_ev_charge_power_w, _MIN_CHARGE_W, _MAX_CHARGE_W)
        result = limit_battery_discharge(loop, setpoint, state.solar_battery_max_discharge_w)
        if result == 0.0:
            loop._eco_night_stopped_at = _time.monotonic()
            logger.info(
                "Eco night: battery discharge limit exceeded (battery=%.0f W, limit=%.0f W), cooldown %.0f s",
                state.solar_battery_power_w or 0,
                state.solar_battery_max_discharge_w,
                _ECO_DAY_COOLDOWN_S,
            )
        return result


class EcoDayModeHandler(ModeSetpointHandler):
    @property
    def name(self) -> str:
        return "eco_day"

    def compute(self, loop: ModeLoopProtocol) -> float:
        state = loop._state

        if state.solar_battery_soc_pct is not None and (
            state.solar_battery_soc_pct < state.eco_day_min_solar_battery_soc_pct
        ):
            if loop._eco_charging:
                logger.info(
                    "Eco day: pausing charge (home battery SOC %.0f%% < threshold %.0f%%)",
                    state.solar_battery_soc_pct,
                    state.eco_day_min_solar_battery_soc_pct,
                )
                loop._eco_charging = False
            loop._eco_day_battery_full = False
            loop._state_machine.set_mode_state(ChargeModeState.ECO_DAY_SOC_GATE)
            return 0.0

        # Hysteresis for battery-full gate: latch True at eco_day_solar_battery_full_pct,
        # only clear when SOC drops below eco_day_solar_battery_full_exit_pct.
        if state.solar_battery_soc_pct is not None and (
            state.solar_battery_soc_pct >= state.eco_day_solar_battery_full_pct
        ):
            loop._eco_day_battery_full = True
        elif (
            state.solar_battery_soc_pct is not None
            and state.solar_battery_soc_pct < state.eco_day_solar_battery_full_exit_pct
        ):
            loop._eco_day_battery_full = False

        current_mean_grid = mean_grid_power(loop)
        current_mean_battery = mean_battery_power(loop)

        if not loop._eco_charging and loop._eco_day_stopped_at is not None:
            elapsed = _time.monotonic() - loop._eco_day_stopped_at
            if elapsed < _ECO_DAY_COOLDOWN_S:
                loop._state_machine.set_mode_state(ChargeModeState.ECO_DAY_COOLDOWN)
                return 0.0

        if not loop._eco_charging:
            grid_export_met = (
                current_mean_grid is not None and current_mean_grid <= state.eco_day_grid_export_charge_start_w
            )
            battery_charge_met = (
                current_mean_battery is not None and current_mean_battery >= state.eco_day_solar_battery_charge_start_w
            )
            if grid_export_met or battery_charge_met:
                loop._eco_charging = True
                loop._eco_day_setpoint_w = _MIN_CHARGE_W
                loop._eco_day_stopped_at = None
                logger.info(
                    "Eco day: starting charge at %.0f W (mean grid=%.0f W, mean battery=%.0f W)",
                    loop._eco_day_setpoint_w,
                    current_mean_grid if current_mean_grid is not None else 0,
                    current_mean_battery if current_mean_battery is not None else 0,
                )
            else:
                loop._state_machine.set_mode_state(ChargeModeState.ECO_DAY_WAITING_FOR_EXPORT)
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
            loop._state_machine.set_mode_state(ChargeModeState.ECO_DAY_COOLDOWN)
            return 0.0

        if not loop._eco_day_battery_full:
            loop._state_machine.set_mode_state(ChargeModeState.ECO_DAY_MINIMUM)
            return _MIN_CHARGE_W

        ev_power = state.ev_active_power_w
        if ev_power is None or ev_power <= 0:
            loop._eco_day_setpoint_w = _MIN_CHARGE_W
            loop._state_machine.set_mode_state(ChargeModeState.ECO_DAY_MINIMUM)
            return _MIN_CHARGE_W

        battery_power = state.solar_battery_power_w
        if battery_power is not None and battery_power < -_RAMP_DEADBAND_W:
            loop._eco_day_setpoint_w -= state.eco_day_ramp_step_w
        else:
            loop._eco_day_setpoint_w += state.eco_day_ramp_step_w

        loop._eco_day_setpoint_w = clamp(loop._eco_day_setpoint_w, _MIN_CHARGE_W, _MAX_CHARGE_W)
        loop._state_machine.set_mode_state(ChargeModeState.ECO_DAY_RAMPING)
        return loop._eco_day_setpoint_w


_NO_VEHICLE_HANDLER = NoVehicleModeHandler()
_MAX_SOC_BLOCKED_HANDLER = MaxSocBlockedModeHandler()
_MANUAL_HANDLER = ManualModeHandler()
_STANDBY_HANDLER = StandbyModeHandler()
_ECO_VICTRON_DOWN_HANDLER = EcoVictronDownModeHandler()
_ECO_NIGHT_HANDLER = EcoNightModeHandler()
_ECO_DAY_HANDLER = EcoDayModeHandler()


def resolve_mode_handler(loop: ModeLoopProtocol) -> ModeSetpointHandler:
    """Resolve the top-level mode strategy for the current loop snapshot."""
    mode = loop._state.charge_mode
    if mode == "Standby":
        return _STANDBY_HANDLER

    if not loop._state.ev_connected or not loop._state.ev_comm_healthy:
        return _NO_VEHICLE_HANDLER

    ev_soc = get_ev_soc(loop)
    margin = _EV_MAX_SOC_MARGIN_PCT if loop._state.ev_max_soc_pct >= 100.0 else 0.0
    if ev_soc is not None and ev_soc >= (loop._state.ev_max_soc_pct - margin):
        return _MAX_SOC_BLOCKED_HANDLER

    if mode == "Manual":
        return _MANUAL_HANDLER

    if not loop._victron_client.connected:
        return _ECO_VICTRON_DOWN_HANDLER

    _throttle.clear("eco_victron_down")

    if is_within_discharge_window(loop._state):
        return _ECO_NIGHT_HANDLER
    return _ECO_DAY_HANDLER


def compute_setpoint(loop: ModeLoopProtocol) -> float:
    """Compute the current charge-power setpoint."""
    return resolve_mode_handler(loop).compute(loop)


def setpoint_manual(loop: ModeLoopProtocol) -> float:
    """Manual: charge at a fixed user-configured power."""
    return _MANUAL_HANDLER.compute(loop)


def setpoint_standby(loop: ModeLoopProtocol) -> float:
    """Standby: no charging."""
    return _STANDBY_HANDLER.compute(loop)


def setpoint_eco_night(loop: ModeLoopProtocol) -> float:
    """Eco inside discharge window: draw from solar battery at a fixed rate."""
    return _ECO_NIGHT_HANDLER.compute(loop)


def setpoint_eco_day(loop: ModeLoopProtocol) -> float:
    """Eco outside discharge window: charge from excess solar."""
    return _ECO_DAY_HANDLER.compute(loop)
