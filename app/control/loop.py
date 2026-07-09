"""Control loop orchestration for EV charger power management."""

import asyncio
import logging
import time as _time

from app.config import ConfigManager
from app.control.constants import (
    _BREAKER_SAFETY_FRACTION,
    _EV_MAX_SOC_DEFAULT,
    _MIN_CHARGE_W,
    _STOP_PRESET_W,
)
from app.control.mode_strategies import compute_setpoint
from app.control.power_utils import (
    limit_phase_current,
    mean_phase_current,
    record_phase_current_samples,
    record_samples,
)
from app.control.protocols import VictronClientProtocol
from app.control.snapshot import build_snapshot
from app.control.state_machine import (
    ChargingStateMachine,
)
from app.modbus import EVChargerModbusClient
from app.state import AppState, ChargeModeState, ChargeSessionState

logger = logging.getLogger(__name__)


class ControlLoop:
    """Periodic control loop that computes and writes EV charge setpoints."""

    def __init__(
        self,
        state: AppState,
        victron_client: VictronClientProtocol,
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
        self._eco_day_battery_full: bool = False
        self._charging_session_state: ChargeSessionState = ChargeSessionState.IDLE
        self._charge_mode_state: ChargeModeState = ChargeModeState.IDLE
        self._stopping_at: float | None = None
        self._stopping_reason: str | None = None
        self._stopped_at: float | None = None
        self._last_positive_setpoint: float = _MIN_CHARGE_W
        self._session_origin_mode: str | None = None
        self._external_stop_ticks: int = 0
        self._start_time: float = _time.monotonic()
        self._eco_day_stopped_at: float | None = None
        self._eco_night_stopped_at: float | None = None
        self._standby_write_quiet: bool = False
        self._grid_power_samples: list[tuple[float, float]] = []
        self._battery_power_samples: list[tuple[float, float]] = []
        self._l1_current_samples: list[tuple[float, float]] = []
        self._l2_current_samples: list[tuple[float, float]] = []
        self._l3_current_samples: list[tuple[float, float]] = []
        self._breaker_cap_tripped: bool = False
        self._state_machine = ChargingStateMachine(self)

    async def _apply_ev_output(self, setpoint: float, suppress_ev_writes: bool) -> None:
        if suppress_ev_writes:
            return

        if setpoint > 0:
            await self._ev_client.write_setpoint(setpoint)
            if self._state_machine.should_send_start_command(setpoint):
                await self._ev_client.start_charging()
            return

        await self._ev_client.write_setpoint(_STOP_PRESET_W)
        if self._state_machine.should_send_stop_command(setpoint):
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

            record_samples(self)
            record_phase_current_samples(self)

            if self._state.ev_connected and self._prev_ev_connected is not True:
                logger.info("EV vehicle connected")
            elif not self._state.ev_connected and self._prev_ev_connected is not False:
                logger.info("EV vehicle disconnected")
                self._eco_charging = False
                self._eco_day_battery_full = False
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

            setpoint = compute_setpoint(self)
            setpoint = self._state_machine.apply_charging_events(setpoint)
            setpoint = limit_phase_current(self, setpoint)
            suppress_ev_writes = self._state_machine.should_suppress_ev_writes(setpoint)
            await self._apply_ev_output(setpoint, suppress_ev_writes)
            self._state.commanded_setpoint_w = setpoint

            # Compute breaker headroom diagnostics for snapshot (FR-11)
            safety_limit = _BREAKER_SAFETY_FRACTION * self._state.grid_breaker_limit_a
            raw_currents = (
                self._state.victron_l1_current_a,
                self._state.victron_l2_current_a,
                self._state.victron_l3_current_a,
            )
            headroom_phases = (
                (1, "l1_breaker_headroom_pct"),
                (2, "l2_breaker_headroom_pct"),
                (3, "l3_breaker_headroom_pct"),
            )
            for phase, attr in headroom_phases:
                raw_i = raw_currents[phase - 1]
                mean_i = mean_phase_current(self, phase)
                if raw_i is not None and mean_i is not None and safety_limit > 0:
                    headroom = (safety_limit - mean_i) / safety_limit * 100.0
                    setattr(self._state, attr, min(headroom, 100.0))
                else:
                    setattr(self._state, attr, None)

            await self._publish_queue.put(build_snapshot(self))
            await asyncio.sleep(self._state.control_loop_interval_s)


__all__ = [
    "ControlLoop",
    "ChargeModeState",
    "ChargeSessionState",
]
