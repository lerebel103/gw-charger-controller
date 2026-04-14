"""Charging state-machine helpers for the control loop."""

from __future__ import annotations

import logging
import time as _time
from enum import Enum
from typing import TYPE_CHECKING

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover - compatibility for local dev on <3.11

    class StrEnum(str, Enum):
        """Compatibility fallback for Python versions without enum.StrEnum."""


from app.control.constants import _EXTERNAL_STOP_CONFIRM_TICKS, _STOPPED_DELAY_S, _STOPPING_MIN_DELAY_S
from app.control.time_utils import is_within_discharge_window
from app.state import ChargerStatus

if TYPE_CHECKING:
    from app.control.loop import ControlLoop

logger = logging.getLogger(__name__)


class ChargeSessionState(StrEnum):
    IDLE = "idle"
    CHARGING = "charging"
    STOPPING = "stopping"
    STOPPED_PENDING = "stopped_pending"


class ChargeModeState(StrEnum):
    IDLE = "idle"
    NO_VEHICLE = "no_vehicle"
    MAX_SOC_BLOCKED = "max_soc_blocked"
    MANUAL = "manual"
    STANDBY = "standby"
    ECO_VICTRON_DOWN = "eco_victron_down"
    ECO_DAY_SOC_GATE = "eco_day_soc_gate"
    ECO_DAY_WAITING_FOR_EXPORT = "eco_day_waiting_for_export"
    ECO_DAY_COOLDOWN = "eco_day_cooldown"
    ECO_DAY_MINIMUM = "eco_day_minimum"
    ECO_DAY_RAMPING = "eco_day_ramping"
    ECO_NIGHT_FLOOR_STOP = "eco_night_floor_stop"
    ECO_NIGHT_BATTERY = "eco_night_battery"
    ECO_NIGHT_GRID_FALLBACK = "eco_night_grid_fallback"


def current_session_state(loop: ControlLoop) -> ChargeSessionState:
    """Return the current session state, honoring legacy raw-string assignments."""
    raw_state = getattr(loop, "_charging_state", None)
    if isinstance(raw_state, str):
        try:
            state = ChargeSessionState(raw_state)
        except ValueError:
            state = getattr(loop, "_charging_session_state", ChargeSessionState.IDLE)
        loop._charging_session_state = state
        return state
    return getattr(loop, "_charging_session_state", ChargeSessionState.IDLE)


def set_session_state(loop: ControlLoop, state: ChargeSessionState) -> None:
    """Update explicit and compatibility session-state fields."""
    previous = current_session_state(loop)
    loop._charging_session_state = state
    loop._charging_state = state.value
    if previous != state:
        logger.info(
            "Charging session state transition: %s -> %s (mode=%s reason=%s setpoint=%.0fW status=%s)",
            previous.value,
            state.value,
            getattr(loop._state, "charge_mode", "unknown"),
            getattr(loop, "_stopping_reason", None),
            float(getattr(loop, "_last_positive_setpoint", 0.0) or 0.0),
            getattr(getattr(loop._state, "ev_charger_status_enum", None), "name", None),
        )


def set_mode_state(loop: ControlLoop, state: ChargeModeState) -> None:
    """Update explicit mode sub-state field."""
    previous = getattr(loop, "_charge_mode_state", ChargeModeState.IDLE)
    loop._charge_mode_state = state
    if previous != state:
        logger.info(
            "Charging mode substate transition: %s -> %s (charge_mode=%s setpoint=%.0fW status=%s)",
            previous.value,
            state.value,
            getattr(loop._state, "charge_mode", "unknown"),
            float(getattr(loop, "_last_positive_setpoint", 0.0) or 0.0),
            getattr(getattr(loop._state, "ev_charger_status_enum", None), "name", None),
        )


def apply_charging_events(loop: ControlLoop, setpoint: float) -> float:
    """Track charging transitions, emit events, and possibly override setpoint."""
    state = loop._state
    wants_to_charge = setpoint > 0

    if current_session_state(loop) == ChargeSessionState.IDLE:
        loop._external_stop_ticks = 0
        if wants_to_charge:
            set_session_state(loop, ChargeSessionState.CHARGING)
            loop._session_origin_mode = state.charge_mode
            loop._last_positive_setpoint = setpoint
            loop._publish_queue.put_nowait(
                {
                    "type": "charging_event",
                    "event": "started",
                    "mode": state.charge_mode,
                    "setpoint_w": setpoint,
                }
            )
            logger.info("Charging event: started (mode=%s, setpoint=%.0f W)", state.charge_mode, setpoint)
        return setpoint

    if current_session_state(loop) == ChargeSessionState.CHARGING:
        if wants_to_charge and loop._last_positive_setpoint > 0 and is_external_stop_candidate(loop):
            loop._external_stop_ticks += 1
            if loop._external_stop_ticks >= _EXTERNAL_STOP_CONFIRM_TICKS:
                reason = determine_stop_reason(loop)
                if reason == "unknown":
                    reason = "external_stop"
                loop._stopping_reason = reason
                set_session_state(loop, ChargeSessionState.STOPPED_PENDING)
                loop._stopped_at = _time.monotonic()
                loop._external_stop_ticks = 0
                logger.info(
                    "Charging event: external stop detected (reason=%s), delaying stopped event %.0f s",
                    reason,
                    _STOPPED_DELAY_S,
                )
                return 0.0
        else:
            loop._external_stop_ticks = 0

        if wants_to_charge:
            loop._last_positive_setpoint = setpoint
            return setpoint

        reason = determine_stop_reason(loop)
        loop._stopping_reason = reason
        mode_switched_during_session = loop._session_origin_mode not in {None, state.charge_mode}
        should_reconcile_mode_switch = mode_switched_during_session and charger_is_active_or_starting(loop)

        loop._publish_queue.put_nowait(
            {
                "type": "charging_event",
                "event": "stopping",
                "mode": state.charge_mode,
                "reason": reason,
                "setpoint_w": loop._last_positive_setpoint,
                "active_power_w": state.ev_active_power_w or 0,
            }
        )

        if should_reconcile_mode_switch:
            set_session_state(loop, ChargeSessionState.STOPPED_PENDING)
            loop._stopped_at = _time.monotonic()
            loop._stopping_at = None
            loop._external_stop_ticks = 0
            logger.info(
                "Charging event: stopping (reason=%s), mode %s->%s during active state "
                "forces setpoint=0 (active_power=%.0f W)",
                reason,
                loop._session_origin_mode,
                state.charge_mode,
                state.ev_active_power_w or 0,
            )
            return 0.0

        set_session_state(loop, ChargeSessionState.STOPPING)
        loop._stopping_at = _time.monotonic()
        loop._external_stop_ticks = 0
        logger.info(
            "Charging event: stopping (reason=%s), holding setpoint for %.0f s (active_power=%.0f W)",
            reason,
            _STOPPING_MIN_DELAY_S,
            state.ev_active_power_w or 0,
        )
        return loop._last_positive_setpoint

    if current_session_state(loop) == ChargeSessionState.STOPPING:
        loop._external_stop_ticks = 0
        if wants_to_charge:
            set_session_state(loop, ChargeSessionState.CHARGING)
            loop._session_origin_mode = state.charge_mode
            loop._last_positive_setpoint = setpoint
            loop._stopping_at = None
            loop._stopping_reason = None
            loop._publish_queue.put_nowait(
                {
                    "type": "charging_event",
                    "event": "started",
                    "mode": state.charge_mode,
                    "setpoint_w": setpoint,
                }
            )
            logger.info("Charging event: started (resumed, mode=%s)", state.charge_mode)
            return setpoint

        elapsed = _time.monotonic() - (loop._stopping_at or 0)
        if elapsed < _STOPPING_MIN_DELAY_S:
            return loop._last_positive_setpoint

        set_session_state(loop, ChargeSessionState.STOPPED_PENDING)
        loop._stopped_at = _time.monotonic()
        logger.info("Charging event: setpoint->0, waiting %.0f s before emitting stopped", _STOPPED_DELAY_S)
        loop._stopping_at = None
        return 0.0

    if current_session_state(loop) == ChargeSessionState.STOPPED_PENDING:
        loop._external_stop_ticks = 0
        if wants_to_charge:
            set_session_state(loop, ChargeSessionState.CHARGING)
            loop._session_origin_mode = state.charge_mode
            loop._last_positive_setpoint = setpoint
            loop._stopped_at = None
            loop._stopping_reason = None
            loop._publish_queue.put_nowait(
                {
                    "type": "charging_event",
                    "event": "started",
                    "mode": state.charge_mode,
                    "setpoint_w": setpoint,
                }
            )
            logger.info("Charging event: started (resumed from stopped_pending, mode=%s)", state.charge_mode)
            return setpoint

        elapsed = _time.monotonic() - (loop._stopped_at or 0)
        if elapsed < _STOPPED_DELAY_S:
            return 0.0

        set_session_state(loop, ChargeSessionState.IDLE)
        ev_soc = loop._get_ev_soc()
        loop._publish_queue.put_nowait(
            {
                "type": "charging_event",
                "event": "stopped",
                "mode": state.charge_mode,
                "reason": loop._stopping_reason or "unknown",
                "session_energy_wh": state.ev_session_energy_wh,
                "ev_soc_pct": ev_soc,
            }
        )
        logger.info(
            "Charging event: stopped (reason=%s, session=%.0f Wh)",
            loop._stopping_reason,
            state.ev_session_energy_wh or 0,
        )
        loop._session_origin_mode = None
        loop._stopped_at = None
        loop._stopping_reason = None
        return 0.0

    return setpoint


def determine_stop_reason(loop: ControlLoop) -> str:
    """Determine why charging is stopping based on current state."""
    state = loop._state
    ev_soc = loop._get_ev_soc()

    if ev_soc is not None and ev_soc >= (state.ev_max_soc_pct - loop._EV_MAX_SOC_MARGIN_PCT):
        return "max_soc_reached"
    if not state.ev_connected:
        return "vehicle_disconnected"
    if state.charge_mode == "Standby":
        return "standby"
    if state.charge_mode == "Eco" and not loop._victron_client.connected:
        return "victron_down"
    if state.charge_mode == "Eco" and not is_within_discharge_window(state):
        soc = state.solar_battery_soc_pct
        if soc is not None and soc < state.eco_day_min_battery_soc_pct:
            return "eco_day_soc_gate"
        mean_battery = loop._mean_battery_power()
        if mean_battery is not None and mean_battery < state.solar_battery_day_power_limit_w:
            return "eco_day_mean_battery"
        return "eco_day_conditions"
    if state.charge_mode == "Eco" and is_within_discharge_window(state):
        return "eco_night_floor"
    return "unknown"


def should_suppress_ev_writes(loop: ControlLoop, setpoint: float) -> bool:
    """Return True when EV Modbus activity should be suppressed in steady standby."""
    if loop._state.charge_mode != "Standby":
        loop._standby_write_quiet = False
        return False

    if setpoint > 0:
        loop._standby_write_quiet = False
        return False

    if loop._standby_write_quiet:
        return True

    if charger_is_active_or_starting(loop):
        return False

    ev_power = loop._state.ev_active_power_w
    if ev_power is not None and ev_power > 0:
        return False

    loop._standby_write_quiet = True
    logger.info("Standby reached - suppressing all EV Modbus interactions (reads, writes, connections)")
    return True


def charger_is_active_or_starting(loop: ControlLoop) -> bool:
    """Return True when charger status indicates charging or start-up activity."""
    status = loop._state.ev_charger_status_enum
    return status in {
        ChargerStatus.HANDSHAKING_WITH_VEHICLE,
        ChargerStatus.CHARGING_IN_PROGRESS,
        ChargerStatus.SCHEDULED_START,
    }


def should_send_start_command(loop: ControlLoop, setpoint: float) -> bool:
    """Return True when a one-shot start command should be sent."""
    if setpoint <= 0 or not loop._state.ev_connected:
        return False

    status = loop._state.ev_charger_status_enum
    if status is None or charger_is_active_or_starting(loop):
        return False

    return status in {
        ChargerStatus.IDLE_CONNECTOR_PLUGGED,
        ChargerStatus.CHARGING_COMPLETED,
        ChargerStatus.START_FAILED,
        ChargerStatus.CHARGING_INTERRUPTED_INSUFFICIENT_PV_BATTERY,
    }


def should_send_stop_command(loop: ControlLoop, setpoint: float) -> bool:
    """Return True when a one-shot stop command should be sent."""
    if setpoint > 0 or not loop._state.ev_connected:
        return False
    return charger_is_active_or_starting(loop)


def is_external_stop_candidate(loop: ControlLoop) -> bool:
    """Return True when charger status indicates charging stopped unexpectedly."""
    status = loop._state.ev_charger_status_enum
    if status is None or status == ChargerStatus.UNKNOWN:
        return False
    if charger_is_active_or_starting(loop):
        return False
    return status in {
        ChargerStatus.IDLE_NO_CONNECTOR,
        ChargerStatus.IDLE_CONNECTOR_PLUGGED,
        ChargerStatus.CHARGING_COMPLETED,
        ChargerStatus.ABNORMAL_ALARM,
        ChargerStatus.MAINTENANCE,
        ChargerStatus.START_FAILED,
        ChargerStatus.CHARGING_INTERRUPTED_INSUFFICIENT_PV_BATTERY,
    }
