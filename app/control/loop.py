"""Control loop orchestration for EV charger power management."""

from __future__ import annotations

import asyncio
import logging
import time as _time

from app.config import ConfigManager
from app.control.constants import (
    _ECO_DAY_COOLDOWN_S,
    _ECO_DAY_RAMP_STEP_W,
    _EV_MAX_SOC_DEFAULT,
    _GRID_EXPORT_START_THRESHOLD_W,
    _MAX_CHARGE_W,
    _MIN_CHARGE_W,
    _STOP_PRESET_W,
)
from app.control.setpoint import (
    clamp,
    compute_grid_fallback_setpoint,
    compute_setpoint,
    get_ev_soc,
    limit_battery_discharge,
    mean_battery_power,
    mean_grid_power,
    prune_samples,
    record_samples,
    setpoint_eco_day,
    setpoint_eco_night,
)
from app.control.snapshot import build_snapshot
from app.control.state_machine import (
    ChargeModeState,
    ChargeSessionState,
    apply_charging_events,
    charger_is_active_or_starting,
    determine_stop_reason,
    is_external_stop_candidate,
    set_mode_state,
    should_send_start_command,
    should_send_stop_command,
    should_suppress_ev_writes,
)
from app.modbus import EVChargerModbusClient

logger = logging.getLogger(__name__)


class ControlLoop:
    """Periodic control loop that computes and writes EV charge setpoints."""

    _EV_MAX_SOC_MARGIN_PCT = 0.5

    def __init__(
        self,
        state,
        victron_client,
        ev_client: EVChargerModbusClient,
        publish_queue: asyncio.Queue,
        config_manager: ConfigManager | None = None,
    ) -> None:
        self._state = state
        self._victron_client = victron_client
        self._ev_client = ev_client
        self._publish_queue = publish_queue
        self._config_manager = config_manager
        self._prev_ev_connected: bool | None = None
        self._eco_charging: bool = False
        self._eco_day_setpoint_w: float = _MIN_CHARGE_W
        self._charging_session_state: ChargeSessionState = ChargeSessionState.IDLE
        self._charging_state: str = self._charging_session_state.value
        self._charge_mode_state: ChargeModeState = ChargeModeState.IDLE
        self._stopping_at: float | None = None
        self._stopping_reason: str | None = None
        self._stopped_at: float | None = None
        self._last_positive_setpoint: float = _MIN_CHARGE_W
        self._session_origin_mode: str | None = None
        self._external_stop_ticks: int = 0
        self._start_time: float = _time.monotonic()
        self._eco_day_stopped_at: float | None = None
        self._standby_write_quiet: bool = False
        self._grid_power_samples: list[tuple[float, float]] = []
        self._battery_power_samples: list[tuple[float, float]] = []

    def _get_ev_soc(self) -> float | None:
        return get_ev_soc(self)

    def _record_samples(self) -> None:
        record_samples(self)

    def _prune_samples(self) -> None:
        prune_samples(self)

    def _mean_grid_power(self) -> float | None:
        return mean_grid_power(self)

    def _mean_battery_power(self) -> float | None:
        return mean_battery_power(self)

    def _compute_setpoint(self) -> float:
        return compute_setpoint(self)

    def _setpoint_manual(self) -> float:
        set_mode_state(self, ChargeModeState.MANUAL)
        return clamp(self._state.manual_power_w, _MIN_CHARGE_W, _MAX_CHARGE_W)

    def _setpoint_standby(self) -> float:
        self._eco_charging = False
        self._eco_day_setpoint_w = _MIN_CHARGE_W
        set_mode_state(self, ChargeModeState.STANDBY)
        return 0.0

    def _setpoint_eco_night(self) -> float:
        return setpoint_eco_night(self)

    def _compute_grid_fallback_setpoint(self, ev_soc: float) -> float:
        return compute_grid_fallback_setpoint(self, ev_soc)

    def _setpoint_eco_day(self) -> float:
        return setpoint_eco_day(self)

    def _apply_charging_events(self, setpoint: float) -> float:
        return apply_charging_events(self, setpoint)

    def _determine_stop_reason(self) -> str:
        return determine_stop_reason(self)

    def _limit_battery_discharge(self, setpoint: float, max_discharge_w: float) -> float:
        return limit_battery_discharge(self, setpoint, max_discharge_w)

    def _should_suppress_ev_writes(self, setpoint: float) -> bool:
        return should_suppress_ev_writes(self, setpoint)

    def _charger_is_active_or_starting(self) -> bool:
        return charger_is_active_or_starting(self)

    def _should_send_start_command(self, setpoint: float) -> bool:
        return should_send_start_command(self, setpoint)

    def _should_send_stop_command(self, setpoint: float) -> bool:
        return should_send_stop_command(self, setpoint)

    def _is_external_stop_candidate(self) -> bool:
        return is_external_stop_candidate(self)

    async def _apply_ev_output(self, setpoint: float, suppress_ev_writes: bool) -> None:
        if suppress_ev_writes:
            return

        if setpoint > 0:
            await self._ev_client.write_setpoint(setpoint)
            if self._should_send_start_command(setpoint):
                await self._ev_client.start_charging()
            return

        await self._ev_client.write_setpoint(_STOP_PRESET_W)
        if self._should_send_stop_command(setpoint):
            await self._ev_client.stop_charging()

    async def run_loop(self) -> None:
        """Master control loop: read -> compute -> write -> publish."""
        while True:
            await self._victron_client.ensure_connected()
            if not self._standby_write_quiet:
                await self._ev_client.ensure_connected()

            await self._victron_client.read()
            if not self._standby_write_quiet:
                await self._ev_client.read()

            self._record_samples()

            if self._state.ev_connected and self._prev_ev_connected is not True:
                logger.info("EV vehicle connected")
            elif not self._state.ev_connected and self._prev_ev_connected is not False:
                logger.info("EV vehicle disconnected")
                if self._state.ev_max_soc_pct != _EV_MAX_SOC_DEFAULT:
                    self._state.ev_max_soc_pct = _EV_MAX_SOC_DEFAULT
                    logger.info("Reset max EV SOC to %.0f%%", _EV_MAX_SOC_DEFAULT)
                if self._state.charge_mode == "Manual":
                    logger.info("Resetting charge mode from Manual to Eco")
                    self._state.charge_mode = "Eco"
                if self._config_manager is not None:
                    self._config_manager.schedule_persist(self._state)
                self._publish_queue.put_nowait("republish_config")
            self._prev_ev_connected = self._state.ev_connected

            setpoint = self._compute_setpoint()
            setpoint = self._apply_charging_events(setpoint)
            suppress_ev_writes = self._should_suppress_ev_writes(setpoint)
            await self._apply_ev_output(setpoint, suppress_ev_writes)
            self._state.commanded_setpoint_w = setpoint

            await self._publish_queue.put(build_snapshot(self))
            await asyncio.sleep(self._state.control_loop_interval_s)


__all__ = [
    "ControlLoop",
    "ChargeModeState",
    "ChargeSessionState",
    "_ECO_DAY_COOLDOWN_S",
    "_ECO_DAY_RAMP_STEP_W",
    "_GRID_EXPORT_START_THRESHOLD_W",
    "_MAX_CHARGE_W",
    "_MIN_CHARGE_W",
    "_STOP_PRESET_W",
]
