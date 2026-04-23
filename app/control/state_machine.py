"""Object-oriented charging state machine for the control loop."""

import logging
import time as _time
from abc import ABC, abstractmethod

from app.control.constants import (
    _EV_MAX_SOC_MARGIN_PCT,
    _EXTERNAL_STOP_CONFIRM_TICKS,
    _STOPPED_DELAY_S,
    _STOPPING_MIN_DELAY_S,
)
from app.control.power_utils import get_ev_soc, mean_battery_power
from app.control.protocols import SessionLoopProtocol
from app.control.time_utils import is_within_discharge_window
from app.state import ChargeModeState, ChargerStatus, ChargeSessionState

logger = logging.getLogger(__name__)


class SessionStateHandler(ABC):
    """Encapsulated behavior for one charging session state."""

    @property
    @abstractmethod
    def state(self) -> ChargeSessionState:
        """Associated enum value for this handler."""

    @abstractmethod
    def handle(self, machine: "ChargingStateMachine", setpoint: float) -> float:
        """Apply state behavior and return the resulting setpoint."""


class IdleSessionStateHandler(SessionStateHandler):
    @property
    def state(self) -> ChargeSessionState:
        return ChargeSessionState.IDLE

    def handle(self, machine: "ChargingStateMachine", setpoint: float) -> float:
        loop = machine.loop
        loop._external_stop_ticks = 0
        if setpoint <= 0:
            return setpoint

        applied = machine.transition_to_charging(setpoint, reset_stop_timers=False)
        machine.emit_started(applied)
        return applied


class ChargingSessionStateHandler(SessionStateHandler):
    @property
    def state(self) -> ChargeSessionState:
        return ChargeSessionState.CHARGING

    def handle(self, machine: "ChargingStateMachine", setpoint: float) -> float:
        loop = machine.loop
        state = loop._state
        wants_to_charge = setpoint > 0

        if wants_to_charge and loop._last_positive_setpoint > 0 and machine.is_external_stop_candidate():
            loop._external_stop_ticks += 1
            if loop._external_stop_ticks >= _EXTERNAL_STOP_CONFIRM_TICKS:
                reason = machine.determine_stop_reason()
                if reason == "unknown":
                    reason = "external_stop"
                loop._stopping_reason = reason
                machine.set_session_state(ChargeSessionState.STOPPED_PENDING)
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

        reason = machine.determine_stop_reason()
        loop._stopping_reason = reason
        mode_switched_during_session = loop._session_origin_mode not in {None, state.charge_mode}
        should_reconcile_mode_switch = mode_switched_during_session and machine.charger_is_active_or_starting()

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
            machine.set_session_state(ChargeSessionState.STOPPED_PENDING)
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

        machine.set_session_state(ChargeSessionState.STOPPING)
        loop._stopping_at = _time.monotonic()
        loop._external_stop_ticks = 0
        logger.info(
            "Charging event: stopping (reason=%s), holding setpoint for %.0f s (active_power=%.0f W)",
            reason,
            _STOPPING_MIN_DELAY_S,
            state.ev_active_power_w or 0,
        )
        return loop._last_positive_setpoint


class StoppingSessionStateHandler(SessionStateHandler):
    @property
    def state(self) -> ChargeSessionState:
        return ChargeSessionState.STOPPING

    def handle(self, machine: "ChargingStateMachine", setpoint: float) -> float:
        loop = machine.loop
        loop._external_stop_ticks = 0
        if setpoint > 0:
            applied = machine.transition_to_charging(setpoint, reset_stop_timers=True)
            machine.emit_started(applied, log_suffix=" (resumed)")
            return applied

        elapsed = _time.monotonic() - (loop._stopping_at or 0)
        if elapsed < _STOPPING_MIN_DELAY_S:
            return loop._last_positive_setpoint

        machine.set_session_state(ChargeSessionState.STOPPED_PENDING)
        loop._stopped_at = _time.monotonic()
        logger.info("Charging event: setpoint->0, waiting %.0f s before emitting stopped", _STOPPED_DELAY_S)
        loop._stopping_at = None
        return 0.0


class StoppedPendingSessionStateHandler(SessionStateHandler):
    @property
    def state(self) -> ChargeSessionState:
        return ChargeSessionState.STOPPED_PENDING

    def handle(self, machine: "ChargingStateMachine", setpoint: float) -> float:
        loop = machine.loop
        state = loop._state
        loop._external_stop_ticks = 0
        if setpoint > 0:
            applied = machine.transition_to_charging(setpoint, reset_stop_timers=True)
            machine.emit_started(applied, log_suffix=" (resumed from stopped_pending)")
            return applied

        elapsed = _time.monotonic() - (loop._stopped_at or 0)
        if elapsed < _STOPPED_DELAY_S:
            return 0.0

        machine.set_session_state(ChargeSessionState.IDLE)
        loop._publish_queue.put_nowait(
            {
                "type": "charging_event",
                "event": "stopped",
                "mode": state.charge_mode,
                "reason": loop._stopping_reason or "unknown",
                "session_energy_wh": state.ev_session_energy_wh,
                "ev_soc_pct": get_ev_soc(loop),
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


class ChargingStateMachine:
    """Owns all charging-state transitions and charger-action decisions."""

    def __init__(self, loop: SessionLoopProtocol) -> None:
        self.loop = loop
        self._session_handlers: dict[ChargeSessionState, SessionStateHandler] = {
            ChargeSessionState.IDLE: IdleSessionStateHandler(),
            ChargeSessionState.CHARGING: ChargingSessionStateHandler(),
            ChargeSessionState.STOPPING: StoppingSessionStateHandler(),
            ChargeSessionState.STOPPED_PENDING: StoppedPendingSessionStateHandler(),
        }

    def current_session_state(self) -> ChargeSessionState:
        """Return current session state."""
        return self.loop._charging_session_state

    def set_session_state(self, state: ChargeSessionState) -> None:
        """Update session-state field."""
        previous = self.current_session_state()
        self.loop._charging_session_state = state
        if previous != state:
            logger.info(
                "Charging session state transition: %s -> %s (mode=%s reason=%s setpoint=%.0fW status=%s)",
                previous.value,
                state.value,
                getattr(self.loop._state, "charge_mode", "unknown"),
                getattr(self.loop, "_stopping_reason", None),
                float(getattr(self.loop, "_last_positive_setpoint", 0.0) or 0.0),
                getattr(getattr(self.loop._state, "ev_charger_status_enum", None), "name", None),
            )

    def set_mode_state(self, state: ChargeModeState) -> None:
        """Update explicit mode sub-state field."""
        previous = getattr(self.loop, "_charge_mode_state", ChargeModeState.IDLE)
        self.loop._charge_mode_state = state
        if previous != state:
            logger.info(
                "Charging mode substate transition: %s -> %s (charge_mode=%s setpoint=%.0fW status=%s)",
                previous.value,
                state.value,
                getattr(self.loop._state, "charge_mode", "unknown"),
                float(getattr(self.loop, "_last_positive_setpoint", 0.0) or 0.0),
                getattr(getattr(self.loop._state, "ev_charger_status_enum", None), "name", None),
            )

    def apply_charging_events(self, setpoint: float) -> float:
        """Track charging transitions, emit events, and possibly override setpoint."""
        return self._session_handlers[self.current_session_state()].handle(self, setpoint)

    def emit_started(self, setpoint: float, *, log_suffix: str = "") -> None:
        state = self.loop._state
        self.loop._publish_queue.put_nowait(
            {
                "type": "charging_event",
                "event": "started",
                "mode": state.charge_mode,
                "setpoint_w": setpoint,
            }
        )
        logger.info("Charging event: started%s (mode=%s)", log_suffix, state.charge_mode)

    def transition_to_charging(self, setpoint: float, *, reset_stop_timers: bool) -> float:
        self.set_session_state(ChargeSessionState.CHARGING)
        self.loop._session_origin_mode = self.loop._state.charge_mode
        self.loop._last_positive_setpoint = setpoint
        if reset_stop_timers:
            self.loop._stopping_at = None
            self.loop._stopped_at = None
            self.loop._stopping_reason = None
        self.loop._external_stop_ticks = 0
        return setpoint

    def determine_stop_reason(self) -> str:
        """Determine why charging is stopping based on current loop state."""
        state = self.loop._state
        ev_soc = get_ev_soc(self.loop)
        margin = _EV_MAX_SOC_MARGIN_PCT if state.ev_max_soc_pct >= 100.0 else 0.0
        if ev_soc is not None and ev_soc >= (state.ev_max_soc_pct - margin):
            return "max_soc_reached"
        if not state.ev_connected:
            return "vehicle_disconnected"
        if state.charge_mode == "Standby":
            return "standby"
        if state.charge_mode == "Eco" and not self.loop._victron_client.connected:
            return "victron_down"
        if state.charge_mode == "Eco" and not is_within_discharge_window(state):
            soc = state.solar_battery_soc_pct
            if soc is not None and soc < state.eco_day_min_battery_soc_pct:
                return "eco_day_soc_gate"
            mean_battery = mean_battery_power(self.loop)
            if mean_battery is not None and mean_battery < state.solar_battery_day_power_limit_w:
                return "eco_day_mean_battery"
            return "eco_day_conditions"
        if state.charge_mode == "Eco" and is_within_discharge_window(state):
            return "eco_night_floor"
        return "unknown"

    def should_suppress_ev_writes(self, setpoint: float) -> bool:
        """Return True when EV Modbus activity should be suppressed in steady standby."""
        if self.loop._state.charge_mode != "Standby":
            self.loop._standby_write_quiet = False
            return False

        if setpoint > 0:
            self.loop._standby_write_quiet = False
            return False

        if self.loop._standby_write_quiet:
            return True

        if self.charger_is_active_or_starting():
            return False

        ev_power = self.loop._state.ev_active_power_w
        if ev_power is not None and ev_power > 0:
            return False

        self.loop._standby_write_quiet = True
        logger.info("Standby reached - suppressing all EV Modbus interactions (reads, writes, connections)")
        return True

    def charger_is_active_or_starting(self) -> bool:
        """Return True when charger status indicates charging or start-up activity."""
        status = self.loop._state.ev_charger_status_enum
        return status in {
            ChargerStatus.HANDSHAKING_WITH_VEHICLE,
            ChargerStatus.CHARGING_IN_PROGRESS,
            ChargerStatus.SCHEDULED_START,
        }

    def should_send_start_command(self, setpoint: float) -> bool:
        """Return True when a one-shot start command should be sent."""
        if setpoint <= 0 or not self.loop._state.ev_connected:
            return False

        status = self.loop._state.ev_charger_status_enum
        if status is None or self.charger_is_active_or_starting():
            return False

        return status in {
            ChargerStatus.IDLE_CONNECTOR_PLUGGED,
            ChargerStatus.CHARGING_COMPLETED,
            ChargerStatus.START_FAILED,
            ChargerStatus.CHARGING_INTERRUPTED_INSUFFICIENT_PV_BATTERY,
        }

    def should_send_stop_command(self, setpoint: float) -> bool:
        """Return True when a one-shot stop command should be sent."""
        if setpoint > 0 or not self.loop._state.ev_connected:
            return False
        return self.charger_is_active_or_starting()

    def is_external_stop_candidate(self) -> bool:
        """Return True when charger status indicates charging stopped unexpectedly."""
        status = self.loop._state.ev_charger_status_enum
        if status is None or status == ChargerStatus.UNKNOWN:
            return False
        if self.charger_is_active_or_starting():
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
